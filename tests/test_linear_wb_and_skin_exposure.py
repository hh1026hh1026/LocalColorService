"""V0.6.2 colour-accuracy changes: A3 (linear statistics), C1 (skin anchor),
C2 (scene-group consensus white balance), C3 (spatial weighting).

Each of these replaces a measurement that was made in the wrong place:

* white balance estimated on display-encoded values, when gray-world is a
  statement about linear-light reflectance
* exposure decided from the whole-frame median, when the frame is mostly sky or
  a dark background and the subject is a face
* a shot whose own estimate was unusable left uncorrected next to corrected
  neighbours in the same lighting setup
* statistics dominated by letterbox bars that the code already knew were there
"""

from __future__ import annotations

import numpy as np
import pytest

from color_core.correction_engine import (
    SKIN_TARGET_BAND,
    generate_auto_correction_advice,
)
from color_core.face_analysis import FaceAnalysis
from color_core.group_white_balance import harmonize_group_white_balance
from color_core.image_analyzer import analyze_image_frames, decode_rec709
from color_core.media_probe import MediaInfo
from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, ShotGrade
from color_core.recipe import GradeRecipe
from color_core.scene_analysis import (
    MAX_REPRESENTATIVE_FRAMES,
    SceneAnalysisResult,
    SceneSegment,
    _representative_times,
    spatial_weight_map,
)


def _tinted(gain_r=1.0, gain_b=1.0, level=0.5, seed=0, size=(96, 96)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.clip(rng.normal(level, 0.14, (*size, 3)), 0.02, 0.98)
    base[..., 2] *= gain_r
    base[..., 0] *= gain_b
    return np.clip(base * 255.0, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# A3 - statistics in linear light
# ---------------------------------------------------------------------------

def test_decode_rec709_round_trips_known_points():
    assert float(decode_rec709(0.0)) == pytest.approx(0.0, abs=1e-6)
    assert float(decode_rec709(1.0)) == pytest.approx(1.0, abs=1e-4)
    # The OETF expands shadows: mid display grey is far darker in linear light.
    # BT.709 puts 0.5 display at about 0.26 linear.
    assert 0.24 < float(decode_rec709(0.5)) < 0.28


def test_report_carries_both_display_and_linear_luminance():
    report = analyze_image_frames([_tinted(level=0.5)])
    assert report.luminance.median > 0.0
    assert report.luminance.linear_median > 0.0
    assert report.luminance.linear_median < report.luminance.median


def test_white_balance_carries_a_physical_illuminant_estimate():
    report = analyze_image_frames([_tinted(gain_r=1.25, gain_b=0.80, seed=3)])
    wb = report.white_balance
    assert wb.source_cct_kelvin > 0
    # A red-heavy frame reads as a warm (low Kelvin) illuminant.
    assert wb.source_cct_kelvin < 6503.5
    assert wb.mired_offset_from_d65 > 0


def test_cool_cast_reads_as_a_high_kelvin_illuminant():
    report = analyze_image_frames([_tinted(gain_r=0.80, gain_b=1.25, seed=4)])
    assert report.white_balance.source_cct_kelvin > 6503.5
    assert report.white_balance.mired_offset_from_d65 < 0


def test_neutral_frame_reports_no_material_cast():
    report = analyze_image_frames([_tinted(seed=5)])
    assert abs(report.white_balance.mired_offset_from_d65) < 12.0


# ---------------------------------------------------------------------------
# C3 - spatial weighting and adaptive sampling
# ---------------------------------------------------------------------------

def test_representative_frame_count_scales_with_duration():
    assert len(_representative_times(0.0, 1.0)) == 3
    assert len(_representative_times(0.0, 60.0)) == MAX_REPRESENTATIVE_FRAMES
    assert len(_representative_times(0.0, 9.0)) > 3


def test_representative_times_stay_inside_the_shot():
    times = _representative_times(10.0, 20.0)
    assert all(10.0 < value < 20.0 for value in times)
    assert times == sorted(times)


def test_centre_is_weighted_above_the_corners():
    weight = spatial_weight_map(np.zeros((100, 200, 3), dtype=np.uint8))
    assert weight[50, 100] > weight[0, 0] * 2.0


def test_letterbox_bars_are_excluded_from_statistics():
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    frame[40:160, :] = 180  # active picture between matte bars
    weight = spatial_weight_map(frame, {"letterbox": True})
    assert weight[5, 100] == 0.0, "top matte bar should carry no weight"
    assert weight[100, 100] > 0.0


def test_weights_change_the_measured_statistics():
    """A letterboxed frame must not report the bars' blackness as content."""
    frame = np.full((200, 200, 3), 160, dtype=np.uint8)
    frame[:40, :] = 0
    frame[160:, :] = 0
    unweighted = analyze_image_frames([frame])
    weighted = analyze_image_frames([frame], [spatial_weight_map(frame, {"letterbox": True})])
    # The median is unmoved here because the active picture is already the
    # majority of pixels; the mean and the clipping figure are what the bars
    # were distorting, and those are what drive exposure decisions.
    assert weighted.luminance.mean > unweighted.luminance.mean
    assert unweighted.clipping.black_clipping_ratio > 0.3
    assert weighted.clipping.black_clipping_ratio < 0.01


def test_all_zero_weights_fall_back_rather_than_failing():
    frame = _tinted()
    weight = spatial_weight_map(frame, {"letterbox": True, "pillarbox": True})
    assert np.any(weight > 0.0)


# ---------------------------------------------------------------------------
# C1 - skin-anchored exposure
# ---------------------------------------------------------------------------

def _dark_skin_face(display_luminance: float) -> FaceAnalysis:
    value = float(display_luminance)
    return FaceAnalysis(
        face_count=1, face_area_ratio=0.08, skin_area_ratio=0.05, confidence=0.8,
        mean_skin_hue=0.05, mean_skin_luminance=value,
        mean_skin_rgb=[value * 1.10, value * 0.88, value * 0.78],
    )


def test_exposure_is_anchored_on_skin_when_a_face_is_present():
    # Frame median says "bright", the face says "far too dark".
    report = analyze_image_frames([_tinted(level=0.75, seed=11)])
    advice = generate_auto_correction_advice(report, face=_dark_skin_face(0.20))
    assert advice.exposure_anchor == "skin_anchor"
    assert advice.suggested_recipe.exposure > 0.0


def test_skin_inside_the_target_band_is_left_alone():
    report = analyze_image_frames([_tinted(level=0.18, seed=12)])
    low, high = SKIN_TARGET_BAND
    advice = generate_auto_correction_advice(report, face=_dark_skin_face((low + high) / 2))
    assert advice.exposure_anchor == "skin_anchor"
    assert advice.suggested_recipe.exposure == 0.0


def test_a_small_face_does_not_hijack_exposure():
    report = analyze_image_frames([_tinted(level=0.5, seed=13)])
    tiny = _dark_skin_face(0.20)
    tiny = tiny.model_copy(update={"face_area_ratio": 0.002})
    advice = generate_auto_correction_advice(report, face=tiny)
    assert advice.exposure_anchor == "frame_median"


def test_exposure_magnitude_is_a_real_ev_figure():
    """Doubling the required luminance must read as about one stop."""
    report = analyze_image_frames([_tinted(level=0.5, seed=14)])
    advice = generate_auto_correction_advice(report, face=_dark_skin_face(0.20))
    # Requested EV is recorded before the safety clamp.
    assert advice.requested_exposure_ev > 1.0


def test_clamped_exposure_is_reported_not_silently_swallowed():
    report = analyze_image_frames([_tinted(level=0.5, seed=15)])
    advice = generate_auto_correction_advice(report, face=_dark_skin_face(0.06))
    assert abs(advice.suggested_recipe.exposure) <= 0.8
    assert advice.requested_exposure_ev > 0.8
    assert any("asked for" in item for item in advice.rationales)


def test_preserve_intent_content_gets_no_exposure_change():
    report = analyze_image_frames([_tinted(level=0.02, seed=16)])
    advice = generate_auto_correction_advice(
        report, content_flags={"preserve_intent": True, "reason": "fade to black"}
    )
    assert advice.suggested_recipe.exposure == 0.0
    assert advice.exposure_anchor == "preserve"


def test_without_a_face_the_frame_median_is_still_used():
    report = analyze_image_frames([_tinted(level=0.12, seed=17)])
    advice = generate_auto_correction_advice(report)
    assert advice.exposure_anchor == "frame_median"
    assert advice.suggested_recipe.exposure > 0.0


# ---------------------------------------------------------------------------
# C2 - scene-group consensus white balance
# ---------------------------------------------------------------------------

def _project_with_group(gains: list[list[float]], reports) -> tuple[ProjectGradeRecipe, SceneAnalysisResult]:
    shots, scenes = [], []
    for index, gain in enumerate(gains):
        shots.append(ShotGrade(
            scene_id=f"scene_{index + 1:04d}", shot_id=f"shot_{index + 1:04d}",
            start_time=index * 2.0, end_time=index * 2.0 + 2.0,
            base_correction=GradeRecipe(rgb_gains=gain),
        ))
        scenes.append(SceneSegment(
            scene_id=f"scene_{index + 1:04d}", index=index,
            start_time=index * 2.0, end_time=index * 2.0 + 2.0,
            start_frame=0, end_frame=1, duration=2.0, analysis=reports[index],
        ))
    project = ProjectGradeRecipe(
        source_path="x.mp4", project_look="neutral_broadcast", shots=shots,
        scene_groups=[SceneGroup(
            scene_group_id="group_0001",
            shot_ids=[shot.shot_id for shot in shots],
            hero_shot_id=shots[0].shot_id,
        )],
    )
    analysis = SceneAnalysisResult(
        detector="synthetic", threshold=3.0,
        media_info=MediaInfo(file_path="x.mp4", width=1920, height=1080), scenes=scenes,
    )
    return project, analysis


def test_a_shot_that_gave_up_adopts_the_group_consensus():
    """The exact failure: one shot uncorrected among corrected neighbours."""
    warm = analyze_image_frames([_tinted(gain_r=1.20, gain_b=0.84, seed=21)])
    confident = [warm, analyze_image_frames([_tinted(gain_r=1.19, gain_b=0.85, seed=22)])]
    unusable = warm.model_copy(deep=True)
    unusable.white_balance.selected_method = "preserve_low_confidence"
    unusable.white_balance.gray_world_confidence = 0.3

    project, analysis = _project_with_group(
        [list(confident[0].white_balance.gain_r for _ in range(1)) and
         [confident[0].white_balance.gain_r, 1.0, confident[0].white_balance.gain_b],
         [confident[1].white_balance.gain_r, 1.0, confident[1].white_balance.gain_b],
         [1.0, 1.0, 1.0]],
        [confident[0], confident[1], unusable],
    )
    before = list(project.shots[2].base_correction.rgb_gains)
    diagnostics = harmonize_group_white_balance(project, analysis)
    after = list(project.shots[2].base_correction.rgb_gains)

    assert before == [1.0, 1.0, 1.0]
    assert after != before, "the unusable shot should have adopted the consensus"
    assert diagnostics[0]["adopted_consensus"] >= 1
    assert diagnostics[0]["consensus"]["cct_kelvin"] > 0


def test_a_confident_shot_keeps_most_of_its_own_reading():
    reports = [
        analyze_image_frames([_tinted(gain_r=1.16, gain_b=0.87, seed=31)]),
        analyze_image_frames([_tinted(gain_r=1.18, gain_b=0.86, seed=32)]),
    ]
    project, analysis = _project_with_group(
        [[item.white_balance.gain_r, 1.0, item.white_balance.gain_b] for item in reports], reports
    )
    original = [list(shot.base_correction.rgb_gains) for shot in project.shots]
    harmonize_group_white_balance(project, analysis)
    for before, shot in zip(original, project.shots):
        after = shot.base_correction.rgb_gains
        assert np.allclose(before, after, atol=0.12), "confident shots should barely move"


def test_a_group_where_nobody_is_confident_is_left_alone():
    reports = []
    for seed in (41, 42):
        report = analyze_image_frames([_tinted(gain_r=1.1, gain_b=0.9, seed=seed)])
        report.white_balance.selected_method = "preserve_low_confidence"
        report.white_balance.gray_world_confidence = 0.2
        reports.append(report)
    project, analysis = _project_with_group([[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]], reports)
    diagnostics = harmonize_group_white_balance(project, analysis)
    assert diagnostics[0]["consensus"] is None
    assert diagnostics[0]["adjusted"] == 0
    assert all(shot.base_correction.rgb_gains == [1.0, 1.0, 1.0] for shot in project.shots)


def test_a_genuinely_different_light_source_is_flagged_not_forced():
    """A confident outlier is usually a practical or a window, not an error."""
    warm = [analyze_image_frames([_tinted(gain_r=1.20, gain_b=0.84, seed=51 + i)]) for i in range(3)]
    cool = analyze_image_frames([_tinted(gain_r=0.80, gain_b=1.24, seed=60)])
    reports = warm + [cool]
    project, analysis = _project_with_group(
        [[item.white_balance.gain_r, 1.0, item.white_balance.gain_b] for item in reports], reports
    )
    outlier_before = list(project.shots[3].base_correction.rgb_gains)
    diagnostics = harmonize_group_white_balance(project, analysis)
    flagged = diagnostics[0]["flagged_different_light"]
    assert flagged, "the cool shot should be flagged"
    entry = next(item for item in flagged if item["shot_id"] == "shot_0004")
    assert abs(entry["delta_mired"]) > 60.0
    if entry["confident"]:
        # A confident outlier is presumed to be a genuinely different light.
        assert entry["action"] == "left_unchanged"
        assert project.shots[3].base_correction.rgb_gains == outlier_before
    else:
        # An uncertain one takes the consensus, but is still surfaced because a
        # correction this large should not be applied silently.
        assert entry["action"] == "pulled_to_consensus"


def test_preserve_policy_shots_are_skipped_entirely():
    reports = [analyze_image_frames([_tinted(gain_r=1.2, gain_b=0.84, seed=71 + i)]) for i in range(2)]
    project, analysis = _project_with_group(
        [[item.white_balance.gain_r, 1.0, item.white_balance.gain_b] for item in reports], reports
    )
    project.shots[1].base_grade_policy = "preserve"
    original = list(project.shots[1].base_correction.rgb_gains)
    harmonize_group_white_balance(project, analysis)
    assert project.shots[1].base_correction.rgb_gains == original

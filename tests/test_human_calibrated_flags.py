"""V0.6.8 - the one QC threshold calibrated against people rather than theory.

A 45-shot review produced a single measurement that separated "needs work" from
"acceptable": the size of the automatic exposure change. Medians were 0.51 EV
against 0.32, AUC 0.715, and a second reviewer showed the same split (0.51
against 0.25).

Nothing else did. Skin hue rotation - the category this replaces on the review
sheet - pointed the wrong way (5.1 degrees for shots needing work, 7.0 for shots
accepted, AUC 0.366), and colour-temperature shift did not separate at all
(AUC 0.52 on absolute magnitude).

Measured on the same reviewed set:

    skin_hue_shift @ 6 deg   precision 0.56   <- barely better than a coin
    highlight_limited        precision 0.71
    large_exposure @ 0.50    precision 0.82
"""

from __future__ import annotations

import numpy as np
import pytest

from color_core.image_analyzer import analyze_image_frames
from color_core.media_probe import MediaInfo
from color_core.project_qc import (
    LARGE_EXPOSURE_CORRECTION_EV,
    SKIN_HUE_ROTATION_LIMIT,
    evaluate_project_quality,
)
from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, ShotGrade
from color_core.recipe import GradeRecipe
from color_core.scene_analysis import SceneAnalysisResult, SceneSegment


def _project(exposures: list[float], diagnostics: list[dict] | None = None):
    shots, scenes = [], []
    frame = np.full((64, 64, 3), 120, np.uint8)
    report = analyze_image_frames([frame])
    for index, exposure in enumerate(exposures):
        recipe = GradeRecipe(exposure=exposure)
        if diagnostics and index < len(diagnostics):
            recipe = recipe.model_copy(update={"exposure_diagnostics": diagnostics[index]})
        shots.append(ShotGrade(
            scene_id=f"scene_{index + 1:04d}", shot_id=f"shot_{index + 1:04d}",
            start_time=index * 2.0, end_time=index * 2.0 + 2.0, base_correction=recipe,
        ))
        scenes.append(SceneSegment(
            scene_id=f"scene_{index + 1:04d}", index=index,
            start_time=index * 2.0, end_time=index * 2.0 + 2.0,
            start_frame=0, end_frame=1, duration=2.0, analysis=report,
        ))
    project = ProjectGradeRecipe(
        source_path="x.mp4", project_look="neutral_broadcast", shots=shots,
        scene_groups=[SceneGroup(
            scene_group_id="group_0001",
            shot_ids=[s.shot_id for s in shots], hero_shot_id=shots[0].shot_id,
        )],
    )
    analysis = SceneAnalysisResult(
        detector="synthetic", threshold=3.0,
        media_info=MediaInfo(file_path="x.mp4", width=1920, height=1080), scenes=scenes,
    )
    return project, analysis


def test_large_exposure_shots_are_flagged():
    project, analysis = _project([0.0, 0.2, 0.55, -0.7, 0.49])
    report = evaluate_project_quality(project, analysis)
    flagged = {item["shot_id"] for item in report.large_exposure_corrections}
    assert flagged == {"shot_0003", "shot_0004"}


def test_the_threshold_is_the_calibrated_value():
    project, analysis = _project([LARGE_EXPOSURE_CORRECTION_EV - 0.01,
                                  LARGE_EXPOSURE_CORRECTION_EV])
    report = evaluate_project_quality(project, analysis)
    flagged = {item["shot_id"] for item in report.large_exposure_corrections}
    assert flagged == {"shot_0002"}


def test_a_requested_change_counts_even_when_the_applied_one_was_cut_back():
    """A shot held back by highlight headroom is exactly the case to surface."""
    project, analysis = _project(
        [0.30],
        [{"requested_ev": 1.8, "applied_ev": 0.30, "headroom_ev": 0.30,
          "highlight_limited": True, "anchor": "skin_anchor"}],
    )
    report = evaluate_project_quality(project, analysis)
    assert len(report.large_exposure_corrections) == 1
    entry = report.large_exposure_corrections[0]
    assert entry["requested_ev"] == pytest.approx(1.8)
    assert entry["highlight_limited"] is True
    assert entry["suggested_action"] == "local_correction"


def test_flagged_shots_carry_a_timecode_for_review():
    project, analysis = _project([0.0, 0.9])
    entry = evaluate_project_quality(project, analysis).large_exposure_corrections[0]
    assert entry["timecode"] == "00:00:02.000"
    assert entry["shot_id"] == "shot_0002"


def test_large_exposure_appears_in_the_review_list():
    project, analysis = _project([0.9])
    report = evaluate_project_quality(project, analysis)
    categories = {item["category"] for item in report.review_items}
    assert "large_exposure_correction" in categories


def test_a_gentle_grade_flags_nothing():
    project, analysis = _project([0.0, 0.1, -0.2, 0.3])
    assert evaluate_project_quality(project, analysis).large_exposure_corrections == []


# ---------------------------------------------------------------------------
# Skin hue gate, deliberately loosened
# ---------------------------------------------------------------------------

def test_skin_hue_gate_no_longer_fires_at_the_unvalidated_six_degrees():
    """6 degrees selected shots nobody objected to; the gate now needs 20."""
    assert SKIN_HUE_ROTATION_LIMIT >= 20.0


def test_skin_hue_is_still_measured_even_though_it_gates_later():
    """Loosening the gate must not remove the number from the report."""
    from color_core.color_metrics import skin_difference_rec709

    difference = skin_difference_rec709([0.70, 0.51, 0.40], [0.70, 0.45, 0.46])
    assert "hue_rotation_deg" in difference
    assert abs(difference["hue_rotation_deg"]) > 0

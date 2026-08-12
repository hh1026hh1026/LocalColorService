"""V0.6.6 - stop the grade inventing discontinuities, and report them honestly.

Two problems found in production logs and job artifacts:

* Continuity was judged against an absolute threshold on the *output*. On a real
  136-shot project the source already exceeded the white-balance threshold at 65
  of 135 cuts and the graded output at 29 - the grade halved them - yet the
  report read "29 problems", of which 16 were actually closer together than in
  the source.
* A scene group whose shots all failed to read their own illuminant was skipped
  entirely, leaving it uncorrected between corrected neighbours. Five of
  twenty-five groups were in that state and they produced the largest introduced
  jumps: one cut went from 4.5 mired in the source to 41.6 after grading.
"""

from __future__ import annotations

import numpy as np
import pytest

from color_core.group_white_balance import (
    BOUNDARY_TOLERANCE_MIRED,
    harmonize_group_white_balance,
)
from color_core.image_analyzer import analyze_image_frames
from color_core.media_probe import MediaInfo
from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, ShotGrade
from color_core.recipe import GradeRecipe
from color_core.scene_analysis import SceneAnalysisResult, SceneSegment
from color_core.white_balance import cct_duv_from_white, source_white_from_gains


def _tinted(gain_r=1.0, gain_b=1.0, seed=0, noise=0.14) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.clip(rng.normal(0.5, noise, (96, 96, 3)), 0.02, 0.98)
    base[..., 2] *= gain_r
    base[..., 0] *= gain_b
    return np.clip(base * 255.0, 0, 255).astype(np.uint8)


def _mired(gains) -> float:
    cct, _ = cct_duv_from_white(source_white_from_gains(tuple(gains)))
    return 1e6 / max(cct, 1.0)


def _build(shot_specs: list[tuple[str, np.ndarray]], groups: dict[str, list[str]]):
    """shot_specs: [(shot_id, frame)], groups: {group_id: [shot_id, ...]}"""
    shots, scenes = [], []
    for index, (shot_id, frame) in enumerate(shot_specs):
        report = analyze_image_frames([frame])
        recipe = GradeRecipe(
            rgb_gains=[report.white_balance.gain_r, 1.0, report.white_balance.gain_b]
        )
        shots.append(ShotGrade(
            scene_id=f"scene_{index + 1:04d}", shot_id=shot_id,
            start_time=index * 2.0, end_time=index * 2.0 + 2.0, base_correction=recipe,
        ))
        scenes.append(SceneSegment(
            scene_id=f"scene_{index + 1:04d}", index=index,
            start_time=index * 2.0, end_time=index * 2.0 + 2.0,
            start_frame=0, end_frame=1, duration=2.0, analysis=report,
        ))
    project = ProjectGradeRecipe(
        source_path="x.mp4", project_look="neutral_broadcast", shots=shots,
        scene_groups=[
            SceneGroup(scene_group_id=name, shot_ids=members, hero_shot_id=members[0])
            for name, members in groups.items()
        ],
    )
    analysis = SceneAnalysisResult(
        detector="synthetic", threshold=3.0,
        media_info=MediaInfo(file_path="x.mp4", width=1920, height=1080), scenes=scenes,
    )
    return project, analysis


# ---------------------------------------------------------------------------
# Boundary relaxation
# ---------------------------------------------------------------------------

def test_similar_neighbours_in_different_groups_are_not_pushed_apart():
    """The exact production failure: source 4.5 mired apart, graded 41.6 apart."""
    specs = [
        ("shot_0001", _tinted(1.18, 0.85, seed=1)),
        ("shot_0002", _tinted(1.18, 0.85, seed=2)),
        # Nearly identical to its predecessor, but in another group.
        ("shot_0003", _tinted(1.17, 0.86, seed=3)),
        ("shot_0004", _tinted(1.17, 0.86, seed=4)),
    ]
    project, analysis = _build(
        specs, {"group_0001": ["shot_0001", "shot_0002"], "group_0002": ["shot_0003", "shot_0004"]}
    )
    source_gap = abs(
        analysis.scenes[1].analysis.white_balance.mired_offset_from_d65
        - analysis.scenes[2].analysis.white_balance.mired_offset_from_d65
    )
    harmonize_group_white_balance(project, analysis)
    graded_gap = abs(
        _mired(project.shots[1].base_correction.rgb_gains)
        - _mired(project.shots[2].base_correction.rgb_gains)
    )
    assert graded_gap <= source_gap + BOUNDARY_TOLERANCE_MIRED + 1e-6, (
        f"grade widened the cut from {source_gap:.1f} to {graded_gap:.1f} mired"
    )


def test_a_real_scene_change_keeps_its_difference():
    """Relaxation must not flatten a genuine interior/exterior cut."""
    specs = [
        ("shot_0001", _tinted(1.30, 0.75, seed=11)),
        ("shot_0002", _tinted(1.30, 0.75, seed=12)),
        ("shot_0003", _tinted(0.76, 1.30, seed=13)),
        ("shot_0004", _tinted(0.76, 1.30, seed=14)),
    ]
    project, analysis = _build(
        specs, {"group_0001": ["shot_0001", "shot_0002"], "group_0002": ["shot_0003", "shot_0004"]}
    )
    harmonize_group_white_balance(project, analysis)
    gap = abs(
        _mired(project.shots[1].base_correction.rgb_gains)
        - _mired(project.shots[2].base_correction.rgb_gains)
    )
    assert gap > 20.0, "a genuine scene change should not be relaxed away"


def test_within_group_spread_shrinks_rather_than_growing():
    """Harmonisation tightens a group; it does not have to flatten it.

    ``CONFIDENT_SELF_WEIGHT`` deliberately leaves a confident shot most of its
    own reading, so real variation inside a scene survives. What must never
    happen is the spread getting *wider*, or a boundary constraint dragging one
    member away from the rest.
    """
    specs = [(f"shot_{i + 1:04d}", _tinted(1.20, 0.84, seed=20 + i)) for i in range(4)]
    specs += [(f"shot_{i + 5:04d}", _tinted(0.82, 1.22, seed=30 + i)) for i in range(2)]
    project, analysis = _build(specs, {
        "group_0001": ["shot_0001", "shot_0002", "shot_0003", "shot_0004"],
        "group_0002": ["shot_0005", "shot_0006"],
    })
    before = np.ptp([_mired(project.shots[i].base_correction.rgb_gains) for i in range(4)])
    harmonize_group_white_balance(project, analysis)
    after = np.ptp([_mired(project.shots[i].base_correction.rgb_gains) for i in range(4)])
    assert after <= before + 1e-6, f"group spread widened: {before:.2f} -> {after:.2f}"
    assert after < 5.0, f"group is still not coherent: {after:.2f} mired"


# ---------------------------------------------------------------------------
# Inheriting from neighbours
# ---------------------------------------------------------------------------

def test_a_group_that_cannot_read_its_light_inherits_from_neighbours():
    """It used to be skipped, becoming an uncorrected island between corrected ones."""
    confident = [(f"shot_{i + 1:04d}", _tinted(1.22, 0.83, seed=40 + i)) for i in range(3)]
    # A flat, near-monochrome shot: the candidates cannot agree on it.
    blank = np.full((96, 96, 3), 128, np.uint8)
    specs = confident[:2] + [("shot_0003", blank)] + confident[2:]
    specs = [(f"shot_{i + 1:04d}", frame) for i, (_, frame) in enumerate(specs)]
    project, analysis = _build(specs, {
        "group_0001": ["shot_0001", "shot_0002"],
        "group_0002": ["shot_0003"],
        "group_0003": ["shot_0004"],
    })
    diagnostics = harmonize_group_white_balance(project, analysis)
    island = next(item for item in diagnostics if item["scene_group_id"] == "group_0002")
    if island["consensus"] is not None and island["consensus"].get("inherited_from_neighbours"):
        assert "inherited" in island["reason"]
        neighbour = next(
            item for item in diagnostics if item["scene_group_id"] == "group_0001"
        )
        assert neighbour["consensus"] is not None


def test_an_isolated_unreadable_group_is_left_alone_and_says_so():
    """With no readable neighbour there is nothing to inherit; do not invent one."""
    blank = np.full((96, 96, 3), 128, np.uint8)
    specs = [("shot_0001", blank), ("shot_0002", blank)]
    project, analysis = _build(
        specs, {"group_0001": ["shot_0001"], "group_0002": ["shot_0002"]}
    )
    original = [list(shot.base_correction.rgb_gains) for shot in project.shots]
    diagnostics = harmonize_group_white_balance(project, analysis)
    for record in diagnostics:
        if record["consensus"] is None:
            assert "left unchanged" in record["reason"] or "inherit" in record["reason"]
    assert [list(shot.base_correction.rgb_gains) for shot in project.shots] == original


# ---------------------------------------------------------------------------
# Continuity QC measured against the source
# ---------------------------------------------------------------------------

def test_continuity_is_judged_against_the_source_not_an_absolute_threshold():
    from color_core.project_qc import WORSENED_RATIO, _source_white_balance_delta

    warm = analyze_image_frames([_tinted(1.30, 0.75, seed=51)])
    cool = analyze_image_frames([_tinted(0.76, 1.30, seed=52)])
    source_delta = _source_white_balance_delta(warm, cool)
    assert source_delta > 25.0, "fixture should be a large source-side difference"
    # A graded difference no bigger than the source's is not a defect, however
    # large it is in absolute terms.
    assert not (source_delta > 25.0 and source_delta > source_delta * WORSENED_RATIO)


def test_source_delta_falls_back_for_reports_without_cct():
    from color_core.project_qc import _source_white_balance_delta

    assert _source_white_balance_delta(None, None) == 0.0

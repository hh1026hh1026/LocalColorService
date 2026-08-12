"""SceneGroup V2: grouping by lighting coherence rather than adjacency.

The V1 grouper walked the shot list once and could only merge a shot into its
immediate predecessor, subject to max_group_shots. Two consequences it locks
down here:

* An A-B-A-B shot/reverse-shot - the most common structure in edited dialogue -
  was broken at every camera change, so one lighting setup landed in several
  groups.
* max_group_shots sat inside the merge condition, so long uniform scenes were
  chopped into fixed-size blocks. The project review saw this as several groups
  containing exactly 12 shots.
"""

from __future__ import annotations

import numpy as np
import pytest

from color_core.image_analyzer import analyze_image_frames
from color_core.media_probe import MediaInfo
from color_core.scene_analysis import SceneAnalysisResult, SceneSegment
from color_core.scene_grouping import (
    MAX_GROUP_MIRED_SPREAD,
    ShotFeature,
    suggest_scene_groups,
)


def _tinted_frame(gain_r: float, gain_b: float, level: float = 0.5, seed: int = 0) -> np.ndarray:
    """Textured frame with a controlled colour cast, in BGR uint8."""
    rng = np.random.default_rng(seed)
    base = np.clip(rng.normal(level, 0.16, size=(96, 96, 3)), 0.02, 0.98)
    base[..., 2] *= gain_r   # R
    base[..., 0] *= gain_b   # B
    return np.clip(base * 255.0, 0, 255).astype(np.uint8)


def _shot(index: int, start: float, end: float, frames: list[np.ndarray]) -> SceneSegment:
    return SceneSegment(
        scene_id=f"scene_{index + 1:04d}", index=index, start_time=start, end_time=end,
        start_frame=int(start * 25), end_frame=int(end * 25), duration=end - start,
        representative_times=[start], analysis=analyze_image_frames(frames),
    )


def _analysis(shots: list[SceneSegment]) -> SceneAnalysisResult:
    return SceneAnalysisResult(
        detector="synthetic", threshold=3.0,
        media_info=MediaInfo(file_path="synthetic.mp4", width=1920, height=1080),
        scenes=shots,
    )


def _shot_reverse_shot() -> SceneAnalysisResult:
    """Six shots alternating between two lighting setups, A B A B A B."""
    setups = [(1.20, 0.80), (0.82, 1.22)]      # warm key vs cool key
    shots = []
    for index in range(6):
        gain_r, gain_b = setups[index % 2]
        shots.append(
            _shot(index, index * 3.0, index * 3.0 + 3.0,
                  [_tinted_frame(gain_r, gain_b, 0.5, seed=100 + index)])
        )
    return _analysis(shots)


def _group_of(groups, shot_id: str) -> str:
    for group in groups:
        if shot_id in group.shot_ids:
            return group.scene_group_id
    raise AssertionError(f"{shot_id} was not assigned to any group")


# ---------------------------------------------------------------------------

def test_every_shot_is_covered_exactly_once():
    analysis = _shot_reverse_shot()
    groups = suggest_scene_groups(analysis)
    assigned = [shot for group in groups for shot in group.shot_ids]
    assert sorted(assigned) == sorted(f"shot_{i + 1:04d}" for i in range(6))
    assert len(assigned) == len(set(assigned))


def test_shot_reverse_shot_recombines_across_the_cut():
    """The V1 regression: A and A' must land together despite B between them."""
    groups = suggest_scene_groups(_shot_reverse_shot())
    warm = {_group_of(groups, f"shot_{i + 1:04d}") for i in (0, 2, 4)}
    cool = {_group_of(groups, f"shot_{i + 1:04d}") for i in (1, 3, 5)}
    assert len(warm) == 1, f"warm setup was split across {warm}"
    assert len(cool) == 1, f"cool setup was split across {cool}"
    assert warm != cool, "the two lighting setups should not share a group"


def test_groups_may_be_non_contiguous_in_time():
    groups = suggest_scene_groups(_shot_reverse_shot())
    target = next(group for group in groups if len(group.shot_ids) > 1)
    indices = sorted(int(item.split("_")[1]) for item in target.shot_ids)
    assert indices != list(range(indices[0], indices[0] + len(indices))), (
        "V2 is expected to produce at least one interleaved group here"
    )


def test_time_window_prevents_distant_shots_from_merging():
    """Visually identical shots far apart in time stay in separate groups."""
    shots = [
        _shot(0, 0.0, 3.0, [_tinted_frame(1.0, 1.0, 0.5, seed=1)]),
        _shot(1, 3.0, 6.0, [_tinted_frame(1.0, 1.0, 0.5, seed=2)]),
        _shot(2, 900.0, 903.0, [_tinted_frame(1.0, 1.0, 0.5, seed=3)]),
    ]
    groups = suggest_scene_groups(_analysis(shots), time_window=45.0)
    assert _group_of(groups, "shot_0001") == _group_of(groups, "shot_0002")
    assert _group_of(groups, "shot_0003") != _group_of(groups, "shot_0001")


def test_max_group_shots_no_longer_chops_uniform_scenes():
    """A long uniform run must not be cut into fixed-size blocks."""
    shots = [
        _shot(i, i * 2.0, i * 2.0 + 2.0, [_tinted_frame(1.0, 1.0, 0.5, seed=200 + i)])
        for i in range(20)
    ]
    groups = suggest_scene_groups(_analysis(shots), max_group_shots=24, time_window=120.0)
    sizes = sorted(len(group.shot_ids) for group in groups)
    assert max(sizes) > 12, f"uniform run was still fragmented: {sizes}"


def test_grouping_is_deterministic():
    analysis = _shot_reverse_shot()
    first = suggest_scene_groups(analysis)
    second = suggest_scene_groups(analysis)
    assert [(g.scene_group_id, g.shot_ids, g.hero_shot_id) for g in first] == [
        (g.scene_group_id, g.shot_ids, g.hero_shot_id) for g in second
    ]


def test_hero_shot_is_the_statistical_centre_not_the_longest():
    """A long outlier must not become the reference every shot is matched to."""
    shots = [
        _shot(0, 0.0, 2.0, [_tinted_frame(1.00, 1.00, 0.50, seed=11)]),
        _shot(1, 2.0, 4.0, [_tinted_frame(1.01, 0.99, 0.50, seed=12)]),
        _shot(2, 4.0, 6.0, [_tinted_frame(0.99, 1.01, 0.50, seed=13)]),
        # Much longer, but sitting at the edge of the group's distribution.
        _shot(3, 6.0, 26.0, [_tinted_frame(1.12, 0.90, 0.50, seed=14)]),
    ]
    groups = suggest_scene_groups(_analysis(shots), similarity_threshold=0.95, time_window=120.0)
    target = next(group for group in groups if "shot_0004" in group.shot_ids)
    if len(target.shot_ids) > 1:
        assert target.hero_shot_id != "shot_0004", "hero should not be the colour outlier"


def test_incoherent_group_is_split_by_the_post_check():
    """The grouper validates its own output and splits what stayed too wide."""
    shots = [
        _shot(0, 0.0, 2.0, [_tinted_frame(1.35, 0.72, 0.5, seed=21)]),
        _shot(1, 2.0, 4.0, [_tinted_frame(1.30, 0.75, 0.5, seed=22)]),
        _shot(2, 4.0, 6.0, [_tinted_frame(0.74, 1.34, 0.5, seed=23)]),
        _shot(3, 6.0, 8.0, [_tinted_frame(0.72, 1.36, 0.5, seed=24)]),
    ]
    # Threshold high enough that naive clustering would merge everything.
    groups = suggest_scene_groups(_analysis(shots), similarity_threshold=5.0, time_window=120.0)
    for group in groups:
        features = [ShotFeature(shots[int(item.split("_")[1]) - 1]) for item in group.shot_ids]
        spread = float(np.ptp([item.mired for item in features]))
        assert spread <= MAX_GROUP_MIRED_SPREAD * 1.5, f"group kept a {spread:.0f} mired spread"


def test_diagnostics_are_recorded_for_audit():
    groups = suggest_scene_groups(_shot_reverse_shot())
    for group in groups:
        assert group.diagnostics["shot_count"] == len(group.shot_ids)
        assert group.diagnostics["median_cct_kelvin"] > 0
        assert "coherent" in group.diagnostics


def test_single_shot_project_returns_one_group():
    shots = [_shot(0, 0.0, 2.0, [_tinted_frame(1.0, 1.0)])]
    groups = suggest_scene_groups(_analysis(shots))
    assert len(groups) == 1
    assert groups[0].hero_shot_id == "shot_0001"


def test_empty_analysis_is_handled():
    assert suggest_scene_groups(_analysis([])) == []

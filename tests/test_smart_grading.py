from pathlib import Path

import cv2
import numpy as np

from color_core.face_analysis import FaceAnalysis
from color_core.grade_decision import decide_grade
from color_core.image_analyzer import analyze_image_frames
from color_core.lut_fitter import fit_lut_from_pairs
from color_core.project_qc import inspect_cube_lut
from color_core.cube_tools import write_cube
from color_core.scene_embedding import scene_embedding
from color_core.selective_renderer import _build_filter


def _gradient(size: int = 96) -> np.ndarray:
    x = np.linspace(0, 255, size, dtype=np.uint8)
    blue, green = np.meshgrid(x, x)
    red = np.full_like(blue, 112)
    return np.stack((blue, green, red), axis=-1)


def test_grade_decision_can_preserve_balanced_source():
    ramp = np.tile(np.linspace(38, 198, 96, dtype=np.uint8), (96, 1))
    frame = np.repeat(ramp[..., None], 3, axis=2)
    report = analyze_image_frames([frame])
    decision = decide_grade(report, FaceAnalysis())
    assert decision.action == "preserve"
    assert decision.recommended_look_strength == 0.0


def test_scene_embedding_is_normalized_and_offline_safe():
    embedding, backend = scene_embedding([_gradient()])
    assert backend in {"offline_hybrid", "dinov2_vits14"}
    assert len(embedding) >= 100
    assert abs(np.linalg.norm(embedding) - 1.0) < 1e-4


def test_lut_fit_scores_exported_strength_on_holdout(tmp_path):
    source = _gradient(128)
    target = source.copy()
    target[..., 2] = np.clip(target[..., 2].astype(np.int16) + (target[..., 1].astype(np.int16) - 128) // 3, 0, 255)
    low = fit_lut_from_pairs([source], [target], str(tmp_path / "low.cube"), strength=0.45, max_samples_per_frame=6000)
    high = fit_lut_from_pairs([source], [target], str(tmp_path / "high.cube"), strength=0.85, max_samples_per_frame=6000)
    assert low["validation_sample_count"] > 0
    assert high["validation_sample_count"] > 0
    assert low["strength"] != high["strength"]
    assert low["fit_rmse"] != high["fit_rmse"]


def test_lut_qc_allows_valid_cross_channel_decrease(tmp_path):
    size = 33
    grid = np.linspace(0.0, 1.0, size)
    blue, green, red = np.meshgrid(grid, grid, grid, indexing="ij")
    # Primary channels remain monotone. Green responding negatively to red is a
    # valid cross-channel color operation and must not be treated as a reversal.
    values = np.stack((red, np.clip(green - red * 0.10, 0.0, 1.0), blue), axis=-1)
    path = write_cube(values, str(tmp_path / "cross_channel.cube"), "cross channel")
    report = inspect_cube_lut(path)
    assert report.passed
    assert report.monotonic_violation_ratio == 0.0


def test_face_selective_filter_has_separate_branches(tmp_path):
    full = str(tmp_path / "full.cube")
    face = str(tmp_path / "face.cube")
    graph = _build_filter([(0.0, 1.0, full, face)])
    assert "maskedmerge" in graph
    assert "full.cube" in graph
    assert "face.cube" in graph

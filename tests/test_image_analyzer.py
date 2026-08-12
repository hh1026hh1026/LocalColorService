"""
Unit Tests for Image Analyzer System.
"""

import pytest
from pathlib import Path
from color_core.frame_sampler import sample_frames
from color_core.image_analyzer import analyze_image_frames, ImageAnalysisReport

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_image_analyzer_neutral():
    video_path = TEST_ASSETS / "neutral_sample.mp4"
    assert video_path.exists()

    frames = sample_frames(str(video_path), num_samples=3)
    report: ImageAnalysisReport = analyze_image_frames(frames)

    assert 0.0 <= report.luminance.mean <= 1.0
    assert 0.0 <= report.luminance.p05 <= report.luminance.p95 <= 1.0
    assert 0.0 <= report.clipping.black_clipping_ratio <= 1.0
    assert 0.0 <= report.clipping.highlight_clipping_ratio <= 1.0
    assert report.contrast.range_p05_p95 > 0.0


def test_image_analyzer_underexposed():
    video_path = TEST_ASSETS / "underexposed_sample.mp4"
    assert video_path.exists()

    frames = sample_frames(str(video_path), num_samples=3)
    report: ImageAnalysisReport = analyze_image_frames(frames)

    assert report.luminance.median < 0.35

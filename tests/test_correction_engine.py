"""
Unit Tests for Auto Correction Engine.
"""

import pytest
from pathlib import Path
from color_core.frame_sampler import sample_frames
from color_core.image_analyzer import analyze_image_frames
from color_core.correction_engine import generate_auto_correction_advice, AutoCorrectionAdvice

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_auto_correction_underexposed():
    video_path = TEST_ASSETS / "underexposed_sample.mp4"
    assert video_path.exists()

    frames = sample_frames(str(video_path), num_samples=3)
    report = analyze_image_frames(frames)
    advice: AutoCorrectionAdvice = generate_auto_correction_advice(report)

    # Should recommend exposure boost
    assert advice.suggested_recipe.exposure > 0.0
    assert -1.0 <= advice.suggested_recipe.exposure <= 1.0
    assert 0.85 <= advice.suggested_recipe.contrast <= 1.20
    assert 0.85 <= advice.suggested_recipe.saturation <= 1.15
    assert len(advice.rationales) > 0
    assert advice.confidence >= 0.50

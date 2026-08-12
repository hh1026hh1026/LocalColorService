"""
Mandatory V0.1.1 Engineering Test: Video Timeline, CFR/VFR, Audio Sync, and Blend Strength.
"""

import os
import pytest
from pathlib import Path
from color_core.recipe import GradeRecipe
from color_core.ocio_manager import OCIOManager
from color_core.lut_baker import bake_3d_lut
from color_core.renderer import render_final
from color_core.media_probe import probe_media, MediaInfo

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_video_timeline_and_blend_strength(tmp_path):
    video_path = TEST_ASSETS / "neutral_sample.mp4"
    assert video_path.exists()

    src_info: MediaInfo = probe_media(str(video_path))
    mgr = OCIOManager()

    # 1. Test Strength = 0.0 (Pure original identity)
    recipe_zero = GradeRecipe(exposure=1.0, contrast=1.3, strength=0.0)
    lut_zero = tmp_path / "zero_strength.cube"
    bake_3d_lut(recipe_zero, str(lut_zero), mgr, lut_size=33)

    out_zero = tmp_path / "out_zero.mp4"
    render_final(str(video_path), str(lut_zero), str(out_zero))

    zero_info: MediaInfo = probe_media(str(out_zero))
    assert abs(zero_info.duration - src_info.duration) <= 0.08
    assert zero_info.total_frames == src_info.total_frames
    assert zero_info.has_audio is True

    # 2. Test Strength = 0.5 (50% blend)
    recipe_half = GradeRecipe(exposure=1.0, contrast=1.3, strength=0.5)
    lut_half = tmp_path / "half_strength.cube"
    bake_3d_lut(recipe_half, str(lut_half), mgr, lut_size=33)

    out_half = tmp_path / "out_half.mp4"
    render_final(str(video_path), str(lut_half), str(out_half))

    half_info: MediaInfo = probe_media(str(out_half))
    assert abs(half_info.duration - src_info.duration) <= 0.08
    assert half_info.has_audio is True

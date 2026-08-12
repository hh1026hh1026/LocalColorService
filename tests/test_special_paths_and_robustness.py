"""
Mandatory V0.1.3 Engineering Test: Special Paths (Chinese, Spaces), Plugin Interface, & Error Robustness.
"""

import os
import shutil
import pytest
from pathlib import Path
from color_core.media_probe import probe_media
from color_core.recipe import GradeRecipe
from color_core.ocio_manager import OCIOManager
from color_core.lut_baker import bake_3d_lut
from color_core.renderer import render_final
from color_core.plugin_interface import TraditionalAutoCorrectionProvider
from color_core.image_analyzer import analyze_image_frames
from color_core.frame_sampler import sample_frames

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_chinese_and_spaces_path_handling(tmp_path):
    cn_dir = tmp_path / "中文 视频 测试 目录"
    cn_dir.mkdir(parents=True, exist_ok=True)

    src_video = TEST_ASSETS / "neutral_sample.mp4"
    target_cn_video = cn_dir / "测试 视频 文件.mp4"
    shutil.copy(str(src_video), str(target_cn_video))

    # Probe media in Chinese path with spaces
    info = probe_media(str(target_cn_video))
    assert info.width == 1280
    assert info.height == 720

    # Bake LUT and Render in Chinese path
    mgr = OCIOManager()
    recipe = GradeRecipe(exposure=0.2)
    lut_path = cn_dir / "中文 滤镜.cube"
    bake_3d_lut(recipe, str(lut_path), mgr, lut_size=33)

    out_cn_video = cn_dir / "输出 调色 视频.mp4"
    render_final(str(target_cn_video), str(lut_path), str(out_cn_video))

    assert out_cn_video.exists()
    assert out_cn_video.stat().st_size > 0


def test_plugin_interface_contract():
    provider = TraditionalAutoCorrectionProvider()
    assert provider.provider_name == "traditional_rule"

    src_video = TEST_ASSETS / "neutral_sample.mp4"
    frames = sample_frames(str(src_video), num_samples=3)
    analysis = analyze_image_frames(frames)

    suggestion = provider.analyze_and_suggest(analysis, context={"source_hash": "test1234"})
    assert suggestion.recommended is True
    assert suggestion.applied is False
    assert 0.0 <= suggestion.confidence <= 1.0
    assert isinstance(suggestion.recipe, GradeRecipe)

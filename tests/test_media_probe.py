"""
Unit Tests for Media Probe System.
"""

import pytest
from pathlib import Path
from color_core.media_probe import probe_media, MediaInfo

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_probe_image():
    img_path = TEST_ASSETS / "sample_image.png"
    assert img_path.exists(), "Run scripts/generate_test_media.py first"

    info: MediaInfo = probe_media(str(img_path))
    assert info.is_image is True
    assert info.width == 1280
    assert info.height == 720
    assert info.total_frames == 1


def test_probe_video():
    video_path = TEST_ASSETS / "neutral_sample.mp4"
    assert video_path.exists(), "Run scripts/generate_test_media.py first"

    info: MediaInfo = probe_media(str(video_path))
    assert info.is_video is True
    assert info.width == 1280
    assert info.height == 720
    assert info.fps == 25.0
    assert info.duration >= 2.9
    assert info.has_audio is True

"""V0.6 A1: the analysis proxy must carry explicit colour metadata.

The 360p analysis proxy was written with no colour tags. At or below 576 lines a
decoder that sees an unspecified matrix commonly assumes BT.601, while the 1080p
source it came from is BT.709. Every statistic the analyzer measured - and
therefore every gray-world rgb_gains it produced - carried that rotation.
"""

from __future__ import annotations

import pytest

from color_core.frame_sampler import analysis_color_tags, color_tag_args
from color_core.media_probe import MediaInfo


def _media(**kwargs) -> MediaInfo:
    base = dict(
        file_path="x.mp4", width=1920, height=1080,
        color_space="unknown", color_primaries="unknown",
        color_transfer="unknown", color_range="unknown",
    )
    base.update(kwargs)
    return MediaInfo(**base)


def test_hd_source_without_tags_falls_back_to_bt709():
    tags = analysis_color_tags(_media())
    assert tags["matrix"] == "bt709"
    assert tags["primaries"] == "bt709"
    assert tags["trc"] == "bt709"
    assert tags["range"] == "tv"
    assert tags["inferred"] == "true"


def test_sd_source_without_tags_falls_back_to_smpte170m():
    tags = analysis_color_tags(_media(width=720, height=480))
    assert tags["matrix"] == "smpte170m"


def test_source_tags_are_preserved_not_overridden():
    tags = analysis_color_tags(
        _media(color_space="bt709", color_primaries="bt709",
               color_transfer="bt709", color_range="pc")
    )
    assert tags["range"] == "pc"
    assert tags["inferred"] == "false"


def test_unrecognised_values_never_reach_ffmpeg():
    """An unknown enum forwarded blindly would fail the encode."""
    tags = analysis_color_tags(
        _media(color_space="reserved", color_primaries="nonsense", color_transfer="???")
    )
    assert tags["matrix"] == "bt709"
    assert tags["primaries"] == "bt709"
    assert tags["trc"] == "bt709"


def test_color_tag_args_emits_all_four_flags():
    args = color_tag_args(analysis_color_tags(_media()))
    for flag in ("-colorspace", "-color_primaries", "-color_trc", "-color_range"):
        assert flag in args
    assert args[args.index("-colorspace") + 1] == "bt709"


def test_probe_exposes_color_range_field():
    assert MediaInfo(file_path="x.mp4").color_range == "unknown"

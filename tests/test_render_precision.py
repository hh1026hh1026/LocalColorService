"""V0.6.2 render precision: float LUT pipeline, chained layers, output profiles.

Every render used to apply ``lut3d`` directly to 8-bit ``yuv420p``. A 3D LUT is
an interpolation over a coarse grid, so evaluating it at 8 bits quantises twice
and the strong Look packages push exactly the smooth gradients where that reads
as banding.

Separately, the technical and creative layers were pre-composed into one LUT.
Measured against a float reference, composition costs about nine times the error
of chaining the two filters.
"""

from __future__ import annotations

import numpy as np
import pytest

from color_core.cube_tools import _sample, compose_cube_luts, read_cube, scale_cube_strength, write_cube
from color_core.output_profiles import (
    LUT_WORKING_FORMAT,
    lut_chain,
    output_pixel_format,
    scaling_args,
    video_encoder_args,
)
from color_core.renderer import build_render_command, get_ffmpeg_executable
from color_core.timeline_renderer import _lut_filters, build_timeline_render_command

ASSET = "test_assets/neutral_sample.mp4"


def _grid(size: int = 17) -> np.ndarray:
    axis = np.linspace(0.0, 1.0, size)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack((red, green, blue), axis=-1)


# ---------------------------------------------------------------------------
# Float pipeline
# ---------------------------------------------------------------------------

def test_lut_runs_in_float_not_eight_bit():
    chain = lut_chain(["lut3d=file='/t/a.cube':interp=tetrahedral"], "delivery")
    assert chain.startswith(f"format={LUT_WORKING_FORMAT}")
    assert chain.endswith("format=yuv420p")


def test_master_profile_keeps_ten_bit_output():
    chain = lut_chain(["lut3d=file='/t/a.cube':interp=tetrahedral"], "master")
    assert chain.endswith("format=yuv422p10le")
    assert output_pixel_format("master") == "yuv422p10le"


def test_eight_bit_output_is_dithered():
    """Dithering is what stops the float-to-8-bit step reintroducing banding."""
    assert "-sws_dither" in scaling_args("delivery")
    assert "-sws_dither" not in scaling_args("master")


def test_timeline_command_uses_the_float_chain():
    command = build_timeline_render_command(ASSET, [(0.0, 1.0, "/t/a.cube")], "/tmp/out.mp4")
    graph = next(item for item in command if "lut3d" in item)
    assert f"format={LUT_WORKING_FORMAT}" in graph
    assert graph.index(LUT_WORKING_FORMAT) < graph.index("lut3d")


def test_single_render_command_uses_the_float_chain(tmp_path):
    lut = write_cube(_grid(), str(tmp_path / "a.cube"), "a")
    command = build_render_command(ASSET, lut, str(tmp_path / "o.mp4"))
    filters = command[command.index("-vf") + 1]
    assert filters.startswith(f"format={LUT_WORKING_FORMAT}")


# ---------------------------------------------------------------------------
# Chained layers
# ---------------------------------------------------------------------------

def test_multiple_layers_become_multiple_lut3d_filters():
    filters = _lut_filters(["/t/a.cube", "/t/b.cube"])
    assert len(filters) == 2
    graph = build_timeline_render_command(
        ASSET, [(0.0, 1.0, ["/t/a.cube", "/t/b.cube"])], "/tmp/o.mp4"
    )
    chain = next(item for item in graph if "lut3d" in item)
    assert chain.count("lut3d") == 2


def test_a_single_path_still_works():
    assert len(_lut_filters("/t/a.cube")) == 1


def test_chaining_is_more_accurate_than_composing(tmp_path):
    """The measurement that motivated the change."""
    technical = np.clip(_grid() * 1.35 - 0.04, 0.0, 1.0)
    creative = np.clip(_grid() ** 1.18 * 0.95, 0.0, 1.0)
    technical_path = write_cube(technical, str(tmp_path / "t.cube"), "t")
    creative_path = write_cube(creative, str(tmp_path / "c.cube"), "c")

    rng = np.random.default_rng(7)
    probe = rng.uniform(0.0, 1.0, (20000, 3))
    truth = _sample(creative, _sample(technical, probe))

    composed = read_cube(compose_cube_luts(technical_path, creative_path, str(tmp_path / "x.cube"), 1.0))
    composed_error = np.abs(_sample(composed, probe) - truth)

    scaled = read_cube(scale_cube_strength(creative_path, 1.0, str(tmp_path / "s.cube")))
    chained_error = np.abs(_sample(scaled, _sample(technical, probe)) - truth)

    assert chained_error.mean() < composed_error.mean() / 3.0
    assert chained_error.max() < composed_error.max() / 3.0


def test_strength_scaling_reduces_to_identity_and_to_the_source(tmp_path):
    creative = np.clip(_grid() ** 1.3, 0.0, 1.0)
    path = write_cube(creative, str(tmp_path / "c.cube"), "c")
    at_zero = read_cube(scale_cube_strength(path, 0.0, str(tmp_path / "z.cube")))
    at_full = read_cube(scale_cube_strength(path, 1.0, str(tmp_path / "f.cube")))
    assert np.allclose(at_zero, _grid(), atol=1e-6)
    assert np.allclose(at_full, creative, atol=1e-6)


def test_strength_scaling_is_monotone(tmp_path):
    creative = np.clip(_grid() * 0.6, 0.0, 1.0)
    path = write_cube(creative, str(tmp_path / "c.cube"), "c")
    previous = None
    for strength in (0.0, 0.25, 0.5, 0.75, 1.0):
        current = read_cube(scale_cube_strength(path, strength, str(tmp_path / f"{strength}.cube")))
        if previous is not None:
            assert current.mean() <= previous.mean() + 1e-9
        previous = current


# ---------------------------------------------------------------------------
# Output profiles
# ---------------------------------------------------------------------------

def test_master_profile_selects_a_high_depth_encoder():
    args, codec = video_encoder_args("master", get_ffmpeg_executable())
    assert codec in ("prores_ks", "dnxhd", "libx265", "libx264")
    if codec != "libx264":
        assert "yuv422p10le" in args


def test_delivery_and_preview_stay_eight_bit():
    for profile in ("delivery", "preview"):
        assert output_pixel_format(profile) == "yuv420p"


def test_master_command_omits_the_h264_bitstream_filter():
    """Applying an H.264 bsf to ProRes or HEVC would abort the render."""
    command = build_timeline_render_command(
        ASSET, [(0.0, 1.0, "/t/a.cube")], "/tmp/o.mov", profile="master"
    )
    assert not any("h264_metadata" in item for item in command)


def test_delivery_command_keeps_the_h264_bitstream_filter():
    command = build_timeline_render_command(ASSET, [(0.0, 1.0, "/t/a.cube")], "/tmp/o.mp4")
    assert any("h264_metadata" in item for item in command)


def test_faststart_is_only_applied_to_mp4():
    mov = build_timeline_render_command(ASSET, [(0.0, 1.0, "/t/a.cube")], "/tmp/o.mov", profile="master")
    mp4 = build_timeline_render_command(ASSET, [(0.0, 1.0, "/t/a.cube")], "/tmp/o.mp4")
    assert "+faststart" not in mov
    assert "+faststart" in mp4

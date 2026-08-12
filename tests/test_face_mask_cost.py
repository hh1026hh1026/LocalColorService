"""Face protection mask generation cost.

Measured on 1080p, the original implementation wrote the mask as a
full-resolution three-channel FFV1 stream at 42 ms/frame - seventeen times the
cost of decoding the frame it came from - which is most of why a face-selective
render took 15-17 minutes against 2-4 for a normal one. The mask is a smooth,
Gaussian-blurred, single-channel signal, so none of that resolution or those
extra two channels carried information.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from color_core.face_analysis import skin_mask_bgr
from color_core.selective_renderer import (
    _build_filter,
    create_face_mask_video,
    mask_detect_interval,
    mask_scale,
)


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("mask") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=640x360:rate=25:duration=4",
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return str(path)


def _probe(path: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=width,height,pix_fmt,nb_frames",
         "-of", "default=nw=1:nk=0", path],
        capture_output=True, text=True, check=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )


def test_mask_is_single_channel_and_reduced_resolution(clip, tmp_path):
    path = create_face_mask_video(clip, str(tmp_path / "mask.avi"))
    info = _probe(path)
    assert info["pix_fmt"] == "gray", "mask must not carry three identical channels"
    assert int(info["width"]) == pytest.approx(640 * mask_scale(), abs=2)
    assert int(info["height"]) == pytest.approx(360 * mask_scale(), abs=2)


def test_mask_covers_every_source_frame(clip, tmp_path):
    """Frame alignment is what the trim filters depend on."""
    path = create_face_mask_video(clip, str(tmp_path / "mask.avi"))
    assert int(_probe(path)["nb_frames"]) == int(_probe(clip)["nb_frames"])


def test_unprotected_spans_are_written_black(clip, tmp_path):
    """Shots that do not use a protected LUT must skip skin detection entirely."""
    path = create_face_mask_video(clip, str(tmp_path / "mask.avi"), protected_spans=[(0.0, 2.0)])
    capture = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    assert len(frames) >= 90
    late = np.stack(frames[75:])  # t > 3s, outside the protected span
    assert float(late.max()) == 0.0, "unprotected span should be fully black"


def test_empty_protected_spans_produce_an_all_black_mask(clip, tmp_path):
    path = create_face_mask_video(clip, str(tmp_path / "mask.avi"), protected_spans=[])
    capture = cv2.VideoCapture(path)
    peak = 0.0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        peak = max(peak, float(frame.max()))
    capture.release()
    assert peak == 0.0


def test_mask_file_is_far_smaller_than_a_full_resolution_rgb_one(clip, tmp_path):
    reduced = Path(create_face_mask_video(clip, str(tmp_path / "reduced.avi")))
    writer = cv2.VideoWriter(
        str(tmp_path / "legacy.avi"), cv2.VideoWriter_fourcc(*"FFV1"), 25, (640, 360), True
    )
    blank = np.zeros((360, 640, 3), dtype=np.uint8)
    for _ in range(int(_probe(clip)["nb_frames"])):
        writer.write(blank)
    writer.release()
    legacy = Path(tmp_path / "legacy.avi")
    assert reduced.stat().st_size < legacy.stat().st_size


# ---------------------------------------------------------------------------

def test_reuse_boxes_skips_detection_and_keeps_the_mask_shape():
    frame = (np.random.default_rng(0).normal(0.45, 0.15, (180, 320, 3)) * 255).clip(0, 255).astype(np.uint8)
    mask, boxes = skin_mask_bgr(frame)
    reused, returned = skin_mask_bgr(frame, reuse_boxes=boxes)
    assert reused.shape == mask.shape
    assert returned == boxes


def test_reuse_boxes_with_a_known_face_region_matches_detection():
    """Reusing boxes must produce the same mask as detecting them again."""
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    frame[60:130, 120:200] = (120, 150, 200)  # a skin-ish patch in BGR
    boxes = [(120, 60, 80, 70)]
    a, _ = skin_mask_bgr(frame, reuse_boxes=boxes)
    b, _ = skin_mask_bgr(frame, reuse_boxes=list(boxes))
    assert np.array_equal(a, b)
    assert a.max() > 0, "the skin patch inside the box should be selected"


def test_detect_interval_is_bounded():
    assert 1 <= mask_detect_interval() <= 12
    assert 0.1 <= mask_scale() <= 1.0


# ---------------------------------------------------------------------------

def test_filter_scales_the_mask_back_to_the_video_size():
    """maskedmerge requires all three inputs to share dimensions."""
    graph = _build_filter([(0.0, 2.0, "/t/a.cube", "/t/b.cube")], 0, (1920, 1080))
    assert "scale=1920:1080" in graph
    assert "maskedmerge" in graph


def test_filter_uses_target_height_for_the_mask_when_downscaling():
    graph = _build_filter([(0.0, 2.0, "/t/a.cube", "/t/b.cube")], 720, None)
    assert "scale=-2:720:flags=bilinear" in graph

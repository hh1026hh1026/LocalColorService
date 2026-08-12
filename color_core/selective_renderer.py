"""Face-aware timeline rendering using a temporally smoothed protection mask."""

from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from color_core.face_analysis import skin_mask_bgr
from color_core.cancellation import raise_if_cancelled, run as cancellable_run
from color_core.media_probe import probe_media
from color_core.output_profiles import LUT_WORKING_FORMAT, scaling_args
from color_core.render_progress import FFmpegProgressMonitor, progress_arguments
from color_core.renderer import (
    _escaped_filter_path, check_encoder_available, frame_rate_mode_args, get_ffmpeg_executable,
)
from color_core.timeline_renderer import _lut_filters


def mask_scale() -> float:
    """Resolution the protection mask is generated at, relative to the source.

    A skin-protection mask is a smooth, low-frequency signal - it is Gaussian
    blurred before use - so generating it at full resolution buys nothing and
    costs a great deal. Measured on 1080p: writing the mask as a full-resolution
    three-channel FFV1 stream cost 42 ms per frame, seventeen times the cost of
    decoding the frame it was derived from. Single channel at quarter
    resolution measured 1.7 ms.
    """
    try:
        value = float(os.getenv("FACE_MASK_SCALE", "0.25"))
    except (TypeError, ValueError):
        return 0.25
    return float(min(1.0, max(0.1, value)))


def mask_detect_interval() -> int:
    """Run the Haar detector every N frames and reuse boxes in between.

    Faces do not move meaningfully in a thirtieth of a second, and the temporal
    smoothing below already absorbs the difference.
    """
    try:
        value = int(os.getenv("FACE_MASK_DETECT_INTERVAL", "3"))
    except (TypeError, ValueError):
        return 3
    return max(1, min(12, value))


def create_face_mask_video(
    input_path: str,
    output_path: str,
    protected_spans: list[tuple[float, float]] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> str:
    """Create a lossless grayscale mask video; white selects the protected branch.

    ``protected_spans`` lists the time ranges that actually use a face-protected
    LUT. Frames outside them are written black without running skin detection at
    all, because whatever the mask says there is discarded by maskedmerge.
    """
    source = cv2.VideoCapture(str(Path(input_path).resolve()))
    if not source.isOpened():
        raise RuntimeError(f"Could not open source for face-mask generation: {input_path}")
    fps = float(source.get(cv2.CAP_PROP_FPS) or 25.0)
    total_frames = max(0, int(source.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
    width = int(source.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(source.get(cv2.CAP_PROP_FRAME_HEIGHT))
    scale = mask_scale()
    mask_width = max(16, int(round(width * scale / 2)) * 2)
    mask_height = max(16, int(round(height * scale / 2)) * 2)
    interval = mask_detect_interval()

    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"FFV1"), fps, (mask_width, mask_height), False
    )
    if not writer.isOpened():
        source.release()
        raise RuntimeError("FFV1 mask writer is unavailable in this OpenCV build")

    def is_protected(index: int) -> bool:
        if protected_spans is None:
            return True
        timestamp = index / max(fps, 1e-6)
        return any(start <= timestamp < end for start, end in protected_spans)

    blank = np.zeros((mask_height, mask_width), dtype=np.uint8)
    previous: np.ndarray | None = None
    boxes: list[tuple[int, int, int, int]] = []
    index = 0
    try:
        while True:
            ok, frame = source.read()
            if not ok:
                break
            if index % 120 == 0:
                # Mask generation is a long in-process loop with no subprocess
                # boundary, so it has to check for cancellation itself.
                raise_if_cancelled()
            if not is_protected(index):
                writer.write(blank)
                previous = None
                index += 1
                if on_progress is not None and index % 30 == 0:
                    on_progress(min(1.0, index / total_frames) if total_frames else 0.0)
                continue
            small = cv2.resize(frame, (mask_width, mask_height), interpolation=cv2.INTER_AREA)
            if index % interval == 0 or not boxes:
                mask, boxes = skin_mask_bgr(small)
            else:
                mask, _ = skin_mask_bgr(small, reuse_boxes=boxes)
            current = mask.astype(np.float32) / 255.0
            if previous is not None:
                # Attack faster than release: protects a newly appearing face
                # promptly while preventing a one-frame detector miss/flicker.
                alpha = np.where(current > previous, 0.60, 0.18)
                current = previous + (current - previous) * alpha
            previous = current
            writer.write(np.clip(current * 255.0 + 0.5, 0, 255).astype(np.uint8))
            index += 1
            if on_progress is not None and index % 30 == 0:
                on_progress(min(1.0, index / total_frames) if total_frames else 0.0)
    finally:
        source.release()
        writer.release()
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Face-mask video was not created")
    if on_progress is not None:
        on_progress(1.0)
    return str(path)


def _build_filter(
    shots: list[tuple[float, float, str, str]],
    target_height: int = 0,
    output_size: tuple[int, int] | None = None,
) -> str:
    count = len(shots)
    source_labels = "".join(f"[src{i}]" for i in range(count))
    mask_labels = "".join(f"[mask{i}]" for i in range(count))
    parts = [f"[0:v]split={count}{source_labels}", f"[1:v]split={count}{mask_labels}"] if count > 1 else []
    outputs = []
    # The mask is generated at reduced resolution, so it has to be scaled back
    # up to the graded branch's size: maskedmerge requires all three inputs to
    # share dimensions and pixel format.
    if output_size:
        mask_scale_filter = f",scale={output_size[0]}:{output_size[1]}:flags=bilinear"
    elif target_height:
        mask_scale_filter = f",scale=-2:{target_height}:flags=bilinear"
    else:
        mask_scale_filter = ""
    for index, (start, end, full_lut, face_lut) in enumerate(shots):
        source_label = f"[src{index}]" if count > 1 else "[0:v]"
        mask_label = f"[mask{index}]" if count > 1 else "[1:v]"
        scale = f",scale=-2:{target_height}:in_range=auto:out_range=tv" if target_height else ""
        # Both branches evaluate their LUT stack in float and meet maskedmerge
        # in gbrp; quantisation happens once, after the merge.
        full = ",".join(_lut_filters(full_lut))
        face = ",".join(_lut_filters(face_lut))
        parts.extend([
            f"{source_label}trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,split=2[fullin{index}][facein{index}]",
            f"[fullin{index}]format={LUT_WORKING_FORMAT},{full}{scale},format=gbrp[full{index}]",
            f"[facein{index}]format={LUT_WORKING_FORMAT},{face}{scale},format=gbrp[face{index}]",
            f"{mask_label}trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS{mask_scale_filter},format=gbrp[maskg{index}]",
            f"[full{index}][face{index}][maskg{index}]maskedmerge,format=yuv420p[out{index}]",
        ])
        outputs.append(f"[out{index}]")
    if count > 1:
        parts.append("".join(outputs) + f"concat=n={count}:v=1:a=0[vout]")
    else:
        parts.append("[out0]null[vout]")
    return ";".join(parts)


def render_face_selective_timeline(
    input_path: str,
    shots: list[tuple[float, float, str, str]],
    output_path: str,
    target_height: int = 0,
    on_progress: Callable[[float], None] | None = None,
) -> str:
    if not shots:
        raise ValueError("At least one face-selective shot is required")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial_output = output.with_name(f"{output.stem}.partial{output.suffix}")
    media = probe_media(input_path)
    # Only the shots whose protected LUT actually differs from the full LUT
    # consume the mask. Everywhere else it can be written black without running
    # skin detection at all.
    def _stack(value) -> tuple[str, ...]:
        """Normalise a shot's LUT entry, which may be one path or a chain.

        Since V0.6.2 a shot can carry a list of LUTs to be chained. This was
        missed here, and ``Path(list)`` raised, so every face-selective render
        silently fell back to the plain timeline - face protection had not been
        running at all.
        """
        items = [value] if isinstance(value, (str, Path)) else list(value)
        return tuple(str(Path(item).resolve()) for item in items)

    protected_spans = [
        (start, end) for start, end, full_lut, face_lut in shots
        if _stack(full_lut) != _stack(face_lut)
    ]
    mask_path = create_face_mask_video(
        input_path, str(output.parent / "face_protection_mask.avi"), protected_spans,
        on_progress=(lambda fraction: on_progress(fraction * 0.20)) if on_progress else None,
    )
    if target_height and media.height:
        width = max(2, int(round(media.width * target_height / media.height / 2)) * 2)
        output_size = (width, target_height)
    else:
        output_size = (media.width, media.height) if media.width and media.height else None
    filter_script = output.parent / "face_selective_filter.ffgraph"
    filter_script.write_text(
        _build_filter(shots, target_height, output_size), encoding="utf-8"
    )
    ffmpeg = get_ffmpeg_executable()
    codecs = ["h264_nvenc", "libx264"] if check_encoder_available(ffmpeg, "h264_nvenc") else ["libx264"]
    failures: list[str] = []
    for codec in codecs:
        command = [ffmpeg, "-y", "-hide_banner", "-i", str(Path(input_path).resolve()), "-i", mask_path,
                   "-filter_complex_script", str(filter_script), "-map", "[vout]"]
        if media.has_audio:
            command += ["-map", "0:a?", "-c:a", "copy"]
        command += scaling_args("delivery")
        if codec == "h264_nvenc":
            command += ["-c:v", codec, "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
        else:
            command += ["-c:v", codec, "-preset", "fast", "-crf", "18"]
        command += ["-r", media.fps_ratio, *frame_rate_mode_args("cfr"), "-color_range", "tv", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-colorspace", "bt709",
                    "-bsf:v", "h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
                    "-movflags", "+faststart", str(partial_output)]
        progress_path = output.parent / "face_ffmpeg_progress.txt"
        if on_progress is not None:
            command = command[:1] + progress_arguments(progress_path) + command[1:]
        monitor = FFmpegProgressMonitor(
            progress_path, total_frames=media.total_frames, duration_seconds=media.duration,
            on_progress=(lambda fraction: on_progress(0.20 + fraction * 0.80)),
        ) if on_progress is not None else nullcontext()
        with monitor:
            result = cancellable_run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200, check=False)
        if result.returncode == 0 and partial_output.is_file() and partial_output.stat().st_size > 0:
            partial_output.replace(output)
            return str(output)
        failures.append(f"{codec}: {result.stderr[-2500:]}")
    raise RuntimeError("Face-selective timeline render failed:\n" + "\n".join(failures))

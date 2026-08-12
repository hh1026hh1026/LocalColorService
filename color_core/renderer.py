"""Reliable FFmpeg LUT rendering for SDR Rec.709 media."""

from __future__ import annotations

import os
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from color_core.cancellation import run as cancellable_run
from color_core.media_probe import MediaInfo, probe_media
from color_core.output_profiles import lut_chain, scaling_args

logger = logging.getLogger("local_color")


def get_ffmpeg_executable() -> str:
    candidates = [
        os.getenv("FFMPEG_PATH", r"C:\ffmpeg_cuda\bin\ffmpeg.exe"),
        os.getenv("FFMPEG_FALLBACK_PATH", ""),
        shutil.which("ffmpeg") or "",
    ]
    return next((str(Path(value).resolve()) for value in candidates if value and Path(value).is_file()), candidates[0])


def check_encoder_available(ffmpeg_exe: str, encoder_name: str) -> bool:
    try:
        result = subprocess.run([ffmpeg_exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30, check=False)
        return result.returncode == 0 and encoder_name in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=4)
def supports_fps_mode(ffmpeg_exe: str) -> bool:
    """Whether this FFmpeg accepts ``-fps_mode`` (5.0+) or needs ``-vsync``.

    Worth detecting rather than assuming: on a build without it every render
    aborts with "Error splitting the argument list: Option not found", which
    says nothing about the cause. It also meant the render path was never
    exercised by the test suite on such a build - four tests had been failing
    for exactly this reason and were being written off as environmental.
    """
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-h", "full"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return "-fps_mode" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def frame_rate_mode_args(mode: str, ffmpeg_exe: str | None = None) -> list[str]:
    """``-fps_mode <mode>`` where available, the legacy ``-vsync`` otherwise."""
    executable = ffmpeg_exe or get_ffmpeg_executable()
    if supports_fps_mode(executable):
        return ["-fps_mode", mode]
    legacy = {"cfr": "cfr", "vfr": "vfr", "passthrough": "passthrough"}
    return ["-vsync", legacy.get(mode, mode)]


def _escaped_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_render_command(
    input_path: str,
    lut_path: str,
    output_path: str,
    target_height: int = 0,
    split_screen: bool = False,
    video_codec: str = "h264_nvenc",
    audio_codec: str = "copy",
) -> list[str]:
    source = Path(input_path).resolve()
    lut = Path(lut_path).resolve()
    output = Path(output_path).resolve()
    info = probe_media(str(source))
    lut_filter = f"lut3d=file='{_escaped_filter_path(lut)}':interp=tetrahedral"
    command = [get_ffmpeg_executable(), "-y", "-hide_banner", "-i", str(source)]
    profile = "delivery"

    if split_screen:
        scale = (
            f"scale=-2:{target_height}:in_range=auto:out_range=tv" if target_height
            else "scale=iw:ih:in_range=auto:out_range=tv"
        )
        graded = lut_chain([lut_filter], profile)
        graph = (
            f"[0:v]split=2[original][gradein];[gradein]{graded}[graded];"
            f"[original][graded]hstack=inputs=2[stack];[stack]{scale},format=yuv420p[vout]"
        )
        command += ["-filter_complex", graph, "-map", "[vout]"]
    else:
        scale = f"scale=-2:{target_height}" if target_height else "scale=iw:ih"
        # The LUT is evaluated in float; only the final conversion quantises.
        filters = lut_chain([lut_filter], profile, extra=f"{scale}:in_range=auto:out_range=tv")
        command += ["-vf", filters, "-map", "0:v:0"]
    if info.has_audio:
        command += ["-map", "0:a?"]
    command += scaling_args(profile)

    if video_codec == "h264_nvenc":
        command += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    elif video_codec == "libx264":
        command += ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    else:
        raise ValueError(f"Unsupported V0.1 video codec: {video_codec}")

    if info.has_audio:
        command += ["-c:a", audio_codec]
        if audio_codec == "aac":
            command += ["-b:a", "192k"]
    command += [
        *frame_rate_mode_args("passthrough"),
        "-color_range", "tv",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-bsf:v", "h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
        "-movflags", "+faststart",
        str(output),
    ]
    return command


def render_media(
    input_path: str,
    lut_path: str,
    output_path: str,
    target_height: int = 0,
    split_screen: bool = False,
    is_preview: bool = False,
) -> str:
    source, lut, output = Path(input_path).resolve(), Path(lut_path).resolve(), Path(output_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input file not found: {source}")
    if not lut.is_file():
        raise FileNotFoundError(f"LUT file not found: {lut}")
    output.parent.mkdir(parents=True, exist_ok=True)
    info: MediaInfo = probe_media(str(source))

    if info.is_image:
        command = [get_ffmpeg_executable(), "-y", "-hide_banner", "-i", str(source), "-vf", f"lut3d=file='{_escaped_filter_path(lut)}':interp=tetrahedral", str(output)]
        result = cancellable_run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False)
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg image render failed: {result.stderr[-4000:]}")
        return str(output)

    ffmpeg = get_ffmpeg_executable()
    preferred = "h264_nvenc" if check_encoder_available(ffmpeg, "h264_nvenc") else "libx264"
    attempts: list[tuple[str, str]] = [(preferred, "copy")]
    if info.has_audio:
        attempts.append((preferred, "aac"))
    if preferred == "h264_nvenc":
        attempts.append(("libx264", "copy"))
        if info.has_audio:
            attempts.append(("libx264", "aac"))

    failures: list[str] = []
    for video_codec, audio_codec in dict.fromkeys(attempts):
        logger.info(f"FFmpeg render attempt video={video_codec} audio={audio_codec} preview={is_preview}")
        command = build_render_command(
            str(source), str(lut), str(output), target_height=target_height,
            split_screen=split_screen, video_codec=video_codec, audio_codec=audio_codec,
        )
        result = cancellable_run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200, check=False)
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            logger.info(f"FFmpeg render succeeded video={video_codec} audio={audio_codec} output={output}")
            return str(output)
        failures.append(f"{video_codec}/{audio_codec}: {result.stderr[-2500:]}")
        logger.warning(f"FFmpeg render attempt failed video={video_codec} audio={audio_codec}: {result.stderr[-600:]}")
    raise RuntimeError("FFmpeg render failed after all codec fallbacks:\n" + "\n".join(failures))


def render_preview(input_path: str, lut_path: str, output_path: str, target_height: int = 720, split_screen: bool = False) -> str:
    return render_media(input_path, lut_path, output_path, target_height, split_screen, True)


def render_final(input_path: str, lut_path: str, output_path: str) -> str:
    return render_media(input_path, lut_path, output_path, 0, False, False)

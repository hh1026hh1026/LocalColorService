"""Single-pass FFmpeg rendering with one LUT per detected shot.

Windows CreateProcess has a short command-line limit. Large projects therefore
store the filter graph in a UTF-8 script and pass only its path to FFmpeg.
"""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from color_core.cancellation import run as cancellable_run
from color_core.render_progress import FFmpegProgressMonitor, progress_arguments
from color_core.media_probe import probe_media
from color_core.output_profiles import lut_chain, scaling_args, video_encoder_args
from color_core.renderer import (
    _escaped_filter_path, check_encoder_available, frame_rate_mode_args, get_ffmpeg_executable,
)


def _lut_filters(lut_paths: str | list[str] | tuple[str, ...]) -> list[str]:
    """One lut3d filter per transform layer, in application order."""
    paths = [lut_paths] if isinstance(lut_paths, (str, Path)) else list(lut_paths)
    if not paths:
        raise ValueError("At least one LUT is required per shot")
    return [
        f"lut3d=file='{_escaped_filter_path(Path(item).resolve())}':interp=tetrahedral"
        for item in paths
    ]


def build_timeline_render_command(
    input_path: str,
    shots: list[tuple[float, float, str]],
    output_path: str,
    video_codec: str = "h264_nvenc",
    audio_codec: str = "copy",
    target_height: int = 0,
    filter_script_path: str | None = None,
    trim_audio: bool = False,
    profile: str = "delivery",
) -> list[str]:
    if not shots:
        raise ValueError("At least one shot LUT is required")
    source = Path(input_path).resolve()
    output = Path(output_path).resolve()
    info = probe_media(str(source))
    labels = "".join(f"[shotin{i}]" for i in range(len(shots)))
    parts = [f"[0:v]split={len(shots)}{labels}"] if len(shots) > 1 else []
    outputs: list[str] = []
    audio_outputs: list[str] = []
    for index, (start, end, lut_path) in enumerate(shots):
        if end <= start:
            raise ValueError(f"Invalid shot interval {index}: {start}..{end}")
        scale = f"scale=-2:{target_height}:in_range=auto:out_range=tv" if target_height else ""
        input_label = f"[shotin{index}]" if len(shots) > 1 else "[0:v]"
        output_label = "vout" if len(shots) == 1 else f"shot{index}"
        # The LUT stack runs in float; only the final conversion is quantised.
        chain = lut_chain(_lut_filters(lut_path), profile, extra=scale)
        parts.append(
            f"{input_label}trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
            f"{chain}[{output_label}]"
        )
        outputs.append(f"[shot{index}]")
        if info.has_audio and trim_audio:
            audio_label = "aout" if len(shots) == 1 else f"audio{index}"
            parts.append(
                f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[{audio_label}]"
            )
            audio_outputs.append(f"[audio{index}]")
    if len(shots) > 1:
        parts.append("".join(outputs) + f"concat=n={len(shots)}:v=1:a=0[vout]")
        if info.has_audio and trim_audio:
            parts.append("".join(audio_outputs) + f"concat=n={len(shots)}:v=0:a=1[aout]")
    filter_graph = ";".join(parts)
    command = [get_ffmpeg_executable(), "-y", "-hide_banner", "-i", str(source)]
    if filter_script_path:
        script = Path(filter_script_path).resolve()
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(filter_graph, encoding="utf-8")
        command += ["-filter_complex_script", str(script)]
    else:
        command += ["-filter_complex", filter_graph]
    command += ["-map", "[vout]"]
    if info.has_audio:
        command += ["-map", "[aout]" if trim_audio else "0:a?"]
    command += scaling_args(profile)
    if profile == "master":
        encoder_args, chosen = video_encoder_args(profile, get_ffmpeg_executable())
        command += encoder_args
    elif video_codec == "h264_nvenc":
        chosen = "h264_nvenc"
        command += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    elif video_codec == "libx264":
        chosen = "libx264"
        command += ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    else:
        raise ValueError(f"Unsupported video codec: {video_codec}")
    if info.has_audio:
        # A master keeps audio uncompressed; re-encoding to AAC for an archive
        # file would be a pointless generation loss.
        if profile == "master" and audio_codec == "aac":
            command += ["-c:a", "pcm_s24le"]
        else:
            command += ["-c:a", audio_codec]
            if audio_codec == "aac":
                command += ["-b:a", "192k"]
    command += [
        "-r", info.fps_ratio, *frame_rate_mode_args("cfr"), "-color_range", "tv", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-colorspace", "bt709",
    ]
    if chosen in ("h264_nvenc", "libx264"):
        # The bitstream filter is H.264-specific; applying it to ProRes or HEVC
        # would abort the render.
        command += [
            "-bsf:v",
            "h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
        ]
    if output.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    command += [str(output)]
    return command


def render_shot_timeline(
    input_path: str,
    shots: list[tuple[float, float, str]],
    output_path: str,
    target_height: int = 0,
    preview: bool = False,
    trim_audio: bool = False,
    profile: str = "",
    on_progress: Callable[[float], None] | None = None,
) -> str:
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial_output = output.with_name(f"{output.stem}.partial{output.suffix}")
    info = probe_media(input_path)
    profile = profile or ("preview" if preview else "delivery")
    ffmpeg = get_ffmpeg_executable()
    # Preview used to force libx264, which saturated the CPU even on systems
    # with NVENC. LUT interpolation remains CPU-bound in this FFmpeg build, but
    # encoding is now consistently offloaded when the GPU encoder is present.
    preferred = "h264_nvenc" if check_encoder_available(ffmpeg, "h264_nvenc") else "libx264"
    attempts = [(preferred, "aac" if trim_audio and info.has_audio else "copy")]
    if info.has_audio and not trim_audio:
        attempts.append((preferred, "aac"))
    if preferred == "h264_nvenc":
        attempts.append(("libx264", "aac" if trim_audio and info.has_audio else "copy"))
        if info.has_audio and not trim_audio:
            attempts.append(("libx264", "aac"))
    failures: list[str] = []
    filter_script = output.parent / "timeline_filter.ffgraph"
    for video_codec, audio_codec in dict.fromkeys(attempts):
        command = build_timeline_render_command(
            input_path, shots, str(partial_output), video_codec, audio_codec, target_height,
            filter_script_path=str(filter_script), trim_audio=trim_audio, profile=profile,
        )
        progress_path = output.parent / "ffmpeg_progress.txt"
        if on_progress is not None:
            command = command[:1] + progress_arguments(progress_path) + command[1:]
        monitor = FFmpegProgressMonitor(
            progress_path, total_frames=info.total_frames, duration_seconds=info.duration,
            on_progress=on_progress,
        ) if on_progress is not None else nullcontext()
        with monitor:
            result = cancellable_run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200, check=False)
        if result.returncode == 0 and partial_output.is_file() and partial_output.stat().st_size > 0:
            partial_output.replace(output)
            return str(output)
        failures.append(f"{video_codec}/{audio_codec}: {result.stderr[-2500:]}")
    raise RuntimeError("FFmpeg shot timeline render failed:\n" + "\n".join(failures))

"""FFprobe-backed media inspection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MediaInfo(BaseModel):
    file_path: str
    is_image: bool = False
    is_video: bool = False
    format_name: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    bit_rate: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    fps_ratio: str = "0/1"
    total_frames: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    audio_streams: int = 0
    has_audio: bool = False
    pix_fmt: str = ""
    color_space: str = "unknown"
    color_primaries: str = "unknown"
    color_transfer: str = "unknown"
    color_range: str = "unknown"
    raw_probe: dict[str, Any] = Field(default_factory=dict)


def get_ffprobe_executable() -> str:
    candidates = [
        os.getenv("FFPROBE_PATH", r"C:\ffmpeg_cuda\bin\ffprobe.exe"),
        os.getenv("FFPROBE_FALLBACK_PATH", ""),
        shutil.which("ffprobe") or "",
    ]
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), candidates[0])


def _ratio(value: str) -> tuple[float, str]:
    try:
        fraction = Fraction(value)
        return float(fraction), f"{fraction.numerator}/{fraction.denominator}"
    except (ValueError, ZeroDivisionError):
        return 0.0, "0/1"


def probe_media(file_path: str) -> MediaInfo:
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source media file not found: {file_path}")
    command = [get_ffprobe_executable(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe failed for {path}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe returned invalid JSON for {path}: {exc}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if video is None:
        raise ValueError(f"No video or image stream found: {path}")
    fmt = data.get("format", {})
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
    is_image = path.suffix.casefold() in image_exts
    duration = float(video.get("duration") or fmt.get("duration") or 0.0)
    fps, fps_ratio = _ratio(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    nb_frames = str(video.get("nb_frames") or "")
    frames = int(nb_frames) if nb_frames.isdigit() else (int(round(duration * fps)) if fps else 1 if is_image else 0)
    return MediaInfo(
        file_path=str(path), is_image=is_image, is_video=not is_image,
        format_name=str(fmt.get("format_name", "")), duration=0.0 if is_image else duration,
        size_bytes=path.stat().st_size, bit_rate=int(fmt.get("bit_rate") or 0),
        width=int(video.get("width") or 0), height=int(video.get("height") or 0),
        fps=round(fps, 6), fps_ratio=fps_ratio, total_frames=1 if is_image else frames,
        video_codec=str(video.get("codec_name", "")),
        audio_codec=str(audios[0].get("codec_name", "")) if audios else "",
        audio_streams=len(audios), has_audio=bool(audios), pix_fmt=str(video.get("pix_fmt", "")),
        color_space=str(video.get("color_space") or "unknown"),
        color_primaries=str(video.get("color_primaries") or "unknown"),
        color_transfer=str(video.get("color_transfer") or "unknown"),
        color_range=str(video.get("color_range") or "unknown"), raw_probe=data,
    )

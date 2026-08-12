"""
Frame Sampler System for Local Color Service
Extracts representative analysis frames from videos or images into NumPy arrays.
"""

import os
import sys
import subprocess
import shutil
import tempfile
import hashlib
import cv2
import numpy as np
from pathlib import Path
from typing import List
from dotenv import load_dotenv

from color_core.cancellation import run as cancellable_run
from color_core.renderer import frame_rate_mode_args
from color_core.media_probe import probe_media

load_dotenv()


def uniform_sample_timestamps(duration: float, num_samples: int) -> List[float]:
    """Return deterministic, uniformly spaced timestamps away from end-of-file."""
    if duration <= 0:
        return [0.0]
    if num_samples <= 1:
        return [duration / 2.0]
    margin = min(0.5, duration * 0.05)
    start_t, end_t = margin, max(margin, duration - margin)
    return np.linspace(start_t, end_t, num_samples).tolist()


def get_ffmpeg_executable() -> str:
    env_path = os.getenv("FFMPEG_PATH", r"C:\ffmpeg_cuda\bin\ffmpeg.exe")
    if os.path.exists(env_path):
        return env_path
    fallback_path = os.getenv("FFMPEG_FALLBACK_PATH", r"E:\ffmpeg-2025-01-15-git-4f3c9f2f03-essentials_build\bin\ffmpeg.exe")
    if os.path.exists(fallback_path):
        return fallback_path
    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path
    return env_path


def _media_cache_key(path: Path, suffix: str = "") -> str:
    stat = path.stat()
    payload = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{suffix}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# FFmpeg's accepted enum values. ffprobe reports the same spellings, but an
# unrecognised string must never be forwarded blindly or the encode will fail.
_MATRIX_VALUES = {
    "bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco", "bt2020nc",
    "bt2020c", "smpte2085", "chroma-derived-nc", "chroma-derived-c", "ictcp",
}
_PRIMARIES_VALUES = {
    "bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020",
    "smpte428", "smpte431", "smpte432", "jedec-p22",
}
_TRC_VALUES = {
    "bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear", "log100",
    "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12",
    "smpte2084", "smpte428", "arib-std-b67",
}
_RANGE_VALUES = {"tv", "pc", "limited", "full"}


def analysis_color_tags(media) -> dict[str, str]:
    """Resolve the colour tags the analysis proxy must carry.

    The proxy used to be written with no colour metadata at all. Because the
    proxy is 360p - at or below the 576-line boundary - a decoder that sees an
    unspecified matrix will commonly fall back to BT.601, while the 1080p source
    it was derived from is BT.709. That mismatch rotates hue and shifts
    saturation on every frame the analyzer measures, and gray-world white
    balance turns the resulting bias straight into an incorrect rgb_gains.

    Tagging the proxy with the source's own coefficients keeps encode and decode
    in agreement, so no conversion error is introduced.
    """
    hd = (getattr(media, "height", 0) or 0) >= 720 or (getattr(media, "width", 0) or 0) >= 1280
    fallback = "bt709" if hd else "smpte170m"
    matrix = media.color_space if media.color_space in _MATRIX_VALUES else fallback
    primaries = media.color_primaries if media.color_primaries in _PRIMARIES_VALUES else fallback
    trc = media.color_transfer if media.color_transfer in _TRC_VALUES else fallback
    color_range = media.color_range if media.color_range in _RANGE_VALUES else "tv"
    return {
        "matrix": matrix, "primaries": primaries, "trc": trc, "range": color_range,
        "inferred": str(media.color_space not in _MATRIX_VALUES).lower(),
    }


def color_tag_args(tags: dict[str, str]) -> list[str]:
    """FFmpeg output arguments that stamp the resolved colour tags."""
    return [
        "-colorspace", tags["matrix"],
        "-color_primaries", tags["primaries"],
        "-color_trc", tags["trc"],
        "-color_range", tags["range"],
    ]


def analysis_cache_root() -> Path:
    root = Path(os.getenv("ANALYSIS_CACHE_DIR", Path(__file__).resolve().parent.parent / "data" / "cache" / "analysis"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def ensure_analysis_proxy(file_path: str, target_height: int = 360) -> tuple[str, dict]:
    """Create one cached low-resolution proxy, preferring NVDEC/CUDA/NVENC.

    Shot detection and representative-frame statistics do not need full source
    resolution. Reusing this proxy avoids repeatedly decoding the camera master.
    """
    source = Path(file_path).resolve()
    media = probe_media(str(source))
    if media.is_image or media.height <= target_height:
        return str(source), {"proxy_used": False, "decode": "source", "scale": "source", "encode": "none"}
    tags = analysis_color_tags(media)
    # The cache suffix is versioned: proxies written before colour tagging and
    # the quality bump must not be reused, or the fix would appear to do nothing.
    cache_dir = analysis_cache_root() / _media_cache_key(source, f"proxy-{target_height}-v2")
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / f"proxy_{target_height}p.mp4"
    proxy_color = {
        "matrix": tags["matrix"], "primaries": tags["primaries"],
        "transfer": tags["trc"], "range": tags["range"],
        "tags_inferred": tags["inferred"] == "true",
    }
    if output.is_file() and output.stat().st_size > 0:
        meta_path = cache_dir / "acceleration.txt"
        mode = meta_path.read_text(encoding="utf-8").strip() if meta_path.is_file() else "cached"
        return str(output), {
            "proxy_used": True, "cached": True, "mode": mode,
            "target_height": target_height, "color": proxy_color,
        }

    ffmpeg = get_ffmpeg_executable()
    color_args = color_tag_args(tags)
    # CQ 28 was visibly quantising the very chroma detail the white-balance
    # estimator depends on. The proxy is small; 20 costs little and measures far
    # closer to the source.
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "20", "-b:v", "0"]
    sws = ["-sws_flags", "+accurate_rnd+full_chroma_int"]
    attempts = [
        (
            "nvdec+scale_cuda+nvenc",
            [ffmpeg, "-y", "-hide_banner", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(source),
             "-an", "-vf", f"scale_cuda=-2:{target_height}", *nvenc, *color_args, str(output)],
        ),
        (
            "cpu_decode+cpu_scale+nvenc",
            [ffmpeg, "-y", "-hide_banner", "-i", str(source), "-an", *sws, "-vf", f"scale=-2:{target_height}",
             *nvenc, *color_args, str(output)],
        ),
        (
            "cpu_fallback",
            [ffmpeg, "-y", "-hide_banner", "-i", str(source), "-an", *sws, "-vf", f"scale=-2:{target_height}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *color_args, str(output)],
        ),
    ]
    failures: list[str] = []
    for mode, command in attempts:
        result = cancellable_run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            (cache_dir / "acceleration.txt").write_text(mode, encoding="utf-8")
            return str(output), {
                "proxy_used": True, "cached": False, "mode": mode,
                "target_height": target_height, "color": proxy_color,
            }
        failures.append(f"{mode}: {result.stderr[-600:]}")
    raise RuntimeError("Could not create analysis proxy:\n" + "\n".join(failures))


def sample_frames(file_path: str, num_samples: int = 5, duration: float = 0.0) -> List[np.ndarray]:
    """
    Extracts N representative frames from a video file, or loads a single image file.
    Returns list of BGR image arrays (uint8).
    """
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Media file not found: {file_path}")

    ext = path.suffix.lower()
    if ext in [".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"]:
        img = cv2.imread(str(path))
        if img is None:
            raise ValueError(f"Failed to read image file: {file_path}")
        return [img]

    ffmpeg_exe = get_ffmpeg_executable()
    frames: List[np.ndarray] = []

    timestamps = uniform_sample_timestamps(duration, num_samples)

    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, ts in enumerate(timestamps):
            out_png = os.path.join(tmpdir, f"frame_{idx:03d}.png")
            cmd = [
                ffmpeg_exe,
                "-y",
                "-ss", f"{ts:.3f}",
                "-i", str(path),
                "-vframes", "1",
                "-pix_fmt", "bgr24",
                out_png
            ]
            try:
                subprocess.run(cmd, capture_output=True, check=True)
                if os.path.exists(out_png):
                    img = cv2.imread(out_png)
                    if img is not None:
                        frames.append(img)
            except Exception as e:
                # If timestamp seek fails, try opening with cv2 VideoCapture fallback
                cap = cv2.VideoCapture(str(path))
                if cap.isOpened():
                    frame_pos = int(ts * cap.get(cv2.CAP_PROP_FPS))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        frames.append(frame)
                    cap.release()

    if not frames:
        # Final fallback: read frame 0 with cv2
        cap = cv2.VideoCapture(str(path))
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                frames.append(frame)
            cap.release()

    if not frames:
        raise RuntimeError(f"Could not extract any sample frames from: {file_path}")

    return frames


def sample_frames_at(file_path: str, timestamps: List[float], target_height: int = 0) -> List[np.ndarray]:
    """Extract explicit frames in one FFmpeg decode and reuse a persistent cache."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {file_path}")
    if path.suffix.lower() in [".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"]:
        return sample_frames(file_path, 1, 0.0)
    if not timestamps:
        return []
    media = probe_media(str(path))
    fps = media.fps or 25.0
    max_frame = max(0, media.total_frames - 1)
    requested_indices = [min(max_frame, max(0, int(round(float(value) * fps)))) for value in timestamps]
    unique_indices = sorted(set(requested_indices))
    # Representative frames feed white-balance statistics AND the CanonCGT LUT
    # fitter. They used to be cached as JPEG q92, whose 4:2:0 chroma subsampling
    # is exactly the signal those consumers measure, so the cache is lossless
    # PNG now. The suffix is versioned to retire the old JPEG cache.
    cache_dir = analysis_cache_root() / _media_cache_key(path, f"frames-{target_height or 'source'}-v2")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached: dict[int, np.ndarray] = {}
    missing: list[int] = []
    for frame_index in unique_indices:
        cached_path = cache_dir / f"frame_{frame_index:09d}.png"
        image = cv2.imread(str(cached_path)) if cached_path.is_file() else None
        if image is None:
            missing.append(frame_index)
        else:
            cached[frame_index] = image

    if missing:
        ffmpeg_exe = get_ffmpeg_executable()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_pattern = str(Path(tmpdir) / "batch_%06d.png")
            expression = "+".join(f"eq(n\\,{index})" for index in missing)
            filters = f"select={expression}"
            if target_height:
                filters += f",scale=-2:{target_height}"
            command = [
                ffmpeg_exe, "-y", "-hide_banner", "-i", str(path), "-vf", filters,
                *frame_rate_mode_args("vfr"), "-pix_fmt", "rgb24", output_pattern,
            ]
            result = cancellable_run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            outputs = sorted(Path(tmpdir).glob("batch_*.png"))
            if result.returncode == 0 and len(outputs) == len(missing):
                for frame_index, output_path in zip(missing, outputs):
                    image = cv2.imread(str(output_path))
                    if image is not None:
                        cached[frame_index] = image
                        cv2.imwrite(str(cache_dir / f"frame_{frame_index:09d}.png"), image)

    # Rare codecs can produce a different frame count near EOF. Retain the
    # proven accurate-seek fallback only for the missing subset.
    for frame_index in unique_indices:
        if frame_index in cached:
            continue
        timestamp = frame_index / fps
        with tempfile.TemporaryDirectory() as tmpdir:
            out_png = Path(tmpdir) / "fallback.png"
            filters = ["-vf", f"scale=-2:{target_height}"] if target_height else []
            command = [get_ffmpeg_executable(), "-y", "-ss", f"{timestamp:.6f}", "-i", str(path), "-frames:v", "1", *filters, str(out_png)]
            result = cancellable_run(command, capture_output=True, check=False)
            image = cv2.imread(str(out_png)) if result.returncode == 0 else None
            if image is not None:
                cached[frame_index] = image
                cv2.imwrite(str(cache_dir / f"frame_{frame_index:09d}.png"), image)
    if any(index not in cached for index in requested_indices):
        raise RuntimeError(f"Could not extract frames at requested timestamps from: {file_path}")
    return [cached[index].copy() for index in requested_indices]

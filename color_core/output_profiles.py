"""Output profiles and the high-precision LUT pipeline (V0.6.2).

Two problems this addresses.

**Precision.** Every render applied ``lut3d`` directly to 8-bit ``yuv420p`` and
wrote 8-bit back out. A 3D LUT is an interpolation over a coarse grid; running
it at 8 bits quantises twice - once on the way in, once on the way out - and the
strong Look packages (``cinematic_soft_print`` has a 0.65 highlight rolloff)
push exactly the smooth gradients where that shows as banding. The LUT now runs
in ``gbrpf32le`` and only the final conversion is quantised, with dithering.

**No master.** The only output was 8-bit H.264. That is a fine delivery file and
a poor archive: regrading it, or taking it into Resolve for finishing, starts
from material that has already been through 4:2:0 chroma subsampling and 8-bit
quantisation. ``master`` writes ProRes 422 HQ (or DNxHR / 10-bit HEVC).
"""

from __future__ import annotations

import subprocess
from functools import lru_cache

# Pixel format the 3D LUT is evaluated in. Verified against the lut3d filter,
# which accepts gbrpf32le, gbrp16le and the yuv444 high-depth variants.
LUT_WORKING_FORMAT = "gbrpf32le"

PROFILE_NAMES = ("preview", "delivery", "master")


@lru_cache(maxsize=8)
def _encoder_available(ffmpeg: str, name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.returncode == 0 and f" {name} " in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def output_pixel_format(profile: str) -> str:
    """Pixel format the encoder receives."""
    if profile == "master":
        return "yuv422p10le"
    return "yuv420p"


def video_encoder_args(profile: str, ffmpeg: str, codec_hint: str = "") -> tuple[list[str], str]:
    """Encoder arguments for a profile, plus the codec actually chosen."""
    if profile == "master":
        if codec_hint in ("", "prores") and _encoder_available(ffmpeg, "prores_ks"):
            # ProRes 422 HQ: the usual interchange master for re-grading.
            return (
                ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0",
                 "-pix_fmt", "yuv422p10le"],
                "prores_ks",
            )
        if codec_hint in ("", "dnxhr") and _encoder_available(ffmpeg, "dnxhd"):
            return (
                ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"],
                "dnxhd",
            )
        if _encoder_available(ffmpeg, "libx265"):
            return (
                ["-c:v", "libx265", "-preset", "slow", "-crf", "14",
                 "-pix_fmt", "yuv422p10le", "-x265-params", "log-level=error"],
                "libx265",
            )
        # Nothing high-depth available; fall back rather than fail the render.
        return (["-c:v", "libx264", "-preset", "slow", "-crf", "14"], "libx264")

    if profile == "preview":
        if _encoder_available(ffmpeg, "h264_nvenc"):
            return (["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"], "h264_nvenc")
        return (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"], "libx264")

    if _encoder_available(ffmpeg, "h264_nvenc"):
        return (["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0"], "h264_nvenc")
    return (["-c:v", "libx264", "-preset", "fast", "-crf", "18"], "libx264")


def lut_chain(lut_filters: list[str], profile: str = "delivery", extra: str = "") -> str:
    """Wrap one or more lut3d filters in a high-precision conversion.

    Chaining rather than pre-composing matters because composition resamples one
    LUT through the other on the 33-point grid; measured against a float
    reference that costs about nine times the error of chaining. See
    ``cube_tools.scale_cube_strength`` for the figures.
    """
    stages = [f"format={LUT_WORKING_FORMAT}"]
    stages.extend(lut_filters)
    if extra:
        stages.append(extra)
    stages.append(f"format={output_pixel_format(profile)}")
    return ",".join(stage for stage in stages if stage)


def scaling_args(profile: str) -> list[str]:
    """Global swscale options.

    Error-diffusion dithering is what keeps the float-to-8-bit step from
    reintroducing the banding the float pipeline just removed.
    """
    args = ["-sws_flags", "+accurate_rnd+full_chroma_int"]
    if output_pixel_format(profile) == "yuv420p":
        args += ["-sws_dither", "ed"]
    return args


def container_suffix(profile: str, codec: str) -> str:
    if profile == "master" and codec in ("prores_ks", "dnxhd"):
        return ".mov"
    return ".mp4"

"""Post-render FFprobe and sampled-image quality checks."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from color_core.frame_sampler import sample_frames
from color_core.image_analyzer import analyze_image_frames
from color_core.media_probe import probe_media


class QualityReport(BaseModel):
    passed: bool
    source_file: str
    rendered_file: str
    output_readable: bool
    output_nonempty: bool
    duration_delta_sec: float = 0.0
    duration_within_one_frame: bool = True
    fps_matches: bool = True
    frame_delta: int = 0
    audio_preserved: bool = True
    metadata_ok: bool = True
    clipping_ok: bool = True
    saturation_ok: bool = True
    color_retention_ok: bool = True
    mean_saturation_ratio: float = 1.0
    mean_luminance_ratio: float = 1.0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    rendered_media_info: dict = Field(default_factory=dict)
    rendered_analysis: dict = Field(default_factory=dict)
    source_analysis: dict = Field(default_factory=dict)


def perform_quality_control(input_path: str, rendered_path: str) -> QualityReport:
    source = probe_media(input_path)
    output_file = Path(rendered_path)
    nonempty = output_file.is_file() and output_file.stat().st_size > 0
    if not nonempty:
        return QualityReport(passed=False, source_file=input_path, rendered_file=rendered_path, output_readable=False, output_nonempty=False, errors=["Rendered output is missing or empty"])
    try:
        output = probe_media(rendered_path)
    except Exception as exc:
        return QualityReport(passed=False, source_file=input_path, rendered_file=rendered_path, output_readable=False, output_nonempty=True, errors=[f"Rendered output is not FFprobe-readable: {exc}"])

    errors: list[str] = []
    warnings: list[str] = []
    frame_duration = 1.0 / source.fps if source.fps else 0.0
    duration_delta = abs(source.duration - output.duration)
    duration_ok = source.is_image or duration_delta <= frame_duration + 0.001
    fps_ok = source.is_image or abs(source.fps - output.fps) <= 0.001
    audio_ok = not source.has_audio or output.has_audio
    expected = {"bt709"}
    metadata_ok = output.color_space.casefold() in expected and output.color_primaries.casefold() in expected and output.color_transfer.casefold() in expected
    if not duration_ok:
        errors.append(f"Duration delta {duration_delta:.6f}s exceeds one source frame ({frame_duration:.6f}s)")
    if not fps_ok:
        errors.append(f"Frame rate changed from {source.fps_ratio} to {output.fps_ratio}")
    if not audio_ok:
        errors.append("Source audio stream was not preserved")
    if not metadata_ok:
        errors.append(f"Missing Rec.709 metadata: primaries={output.color_primaries}, transfer={output.color_transfer}, matrix={output.color_space}")

    frames = sample_frames(rendered_path, num_samples=3, duration=output.duration)
    source_frames = sample_frames(input_path, num_samples=3, duration=source.duration)
    analysis = analyze_image_frames(frames)
    source_analysis = analyze_image_frames(source_frames)
    source_saturation = source_analysis.saturation.mean
    saturation_ratio = analysis.saturation.mean / source_saturation if source_saturation > 0.02 else 1.0
    source_luminance = source_analysis.luminance.mean
    luminance_ratio = analysis.luminance.mean / source_luminance if source_luminance > 0.02 else 1.0
    clipping_ok = (
        analysis.clipping.black_clipping_ratio <= max(0.12, source_analysis.clipping.black_clipping_ratio + 0.08)
        and analysis.clipping.highlight_clipping_ratio <= max(0.06, source_analysis.clipping.highlight_clipping_ratio + 0.04)
    )
    saturation_ok = analysis.saturation.p95 < 0.99 or (
        source_analysis.saturation.p95 >= 0.99 and saturation_ratio <= 1.20
    )
    if not clipping_ok:
        warnings.append("Sampled output added significant clipping relative to the source")
    if not saturation_ok:
        warnings.append("Sampled output added significant saturation relative to the source")
    color_retention_ok = saturation_ratio >= 0.55
    if not color_retention_ok:
        errors.append(f"Severe color loss: mean saturation retained only {saturation_ratio:.2f}x of source")
    elif saturation_ratio < 0.70:
        warnings.append(f"Color retention is low: mean saturation ratio {saturation_ratio:.2f}")
    if luminance_ratio < 0.50 or luminance_ratio > 1.80:
        warnings.append(f"Large mean luminance shift: ratio {luminance_ratio:.2f}")
    return QualityReport(
        passed=not errors, source_file=input_path, rendered_file=rendered_path,
        output_readable=True, output_nonempty=True, duration_delta_sec=round(duration_delta, 6),
        duration_within_one_frame=duration_ok, fps_matches=fps_ok,
        frame_delta=abs(source.total_frames - output.total_frames), audio_preserved=audio_ok,
        metadata_ok=metadata_ok, clipping_ok=clipping_ok, saturation_ok=saturation_ok,
        color_retention_ok=color_retention_ok,
        mean_saturation_ratio=round(saturation_ratio, 4),
        mean_luminance_ratio=round(luminance_ratio, 4),
        warnings=warnings, errors=errors, rendered_media_info=output.model_dump(mode="json"),
        rendered_analysis=analysis.model_dump(mode="json"),
        source_analysis=source_analysis.model_dump(mode="json"),
    )

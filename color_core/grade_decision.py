"""Explainable decision gate for preserving or grading a shot."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from color_core.face_analysis import FaceAnalysis
from color_core.image_analyzer import ImageAnalysisReport


class GradeDecision(BaseModel):
    action: Literal["preserve", "technical_only", "creative_low", "creative_full", "manual_review"]
    technical_need: float = Field(ge=0.0, le=1.0)
    creative_risk: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_look_strength: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


def decide_grade(
    report: ImageAnalysisReport,
    face: FaceAnalysis | None = None,
    content_flags: dict | None = None,
) -> GradeDecision:
    face = face or FaceAnalysis()
    content_flags = content_flags or {}
    lum = report.luminance.median
    exposure_need = min(1.0, max(0.0, 0.25 - lum) / 0.25 + max(0.0, lum - 0.60) / 0.30)
    contrast_need = min(1.0, abs(report.contrast.range_p05_p95 - 0.58) / 0.42)
    wb = report.white_balance
    # Mired is the physically meaningful measure of how far the illuminant is
    # from D65. 45 mired is roughly a quarter CTO, i.e. a clearly visible cast.
    if wb.source_cct_kelvin:
        wb_need = min(1.0, abs(wb.mired_offset_from_d65) / 45.0) * wb.gray_world_confidence
    else:
        wb_need = min(1.0, abs(wb.estimated_temp_offset) / 0.22) * wb.gray_world_confidence
    black_clip = 0.0 if content_flags.get("letterbox") or content_flags.get("pillarbox") else report.clipping.black_clipping_ratio
    clipping = min(1.0, report.clipping.highlight_clipping_ratio / 0.08 + black_clip / 0.20)
    technical_need = min(1.0, 0.40 * exposure_need + 0.20 * contrast_need + 0.25 * wb_need + 0.15 * clipping)

    face_risk = min(0.55, face.face_area_ratio * 2.5) if face.face_count else 0.0
    saturation_risk = min(0.35, max(0.0, report.saturation.p95 - 0.82) * 1.8)
    large_cast = (
        abs(wb.mired_offset_from_d65) > 18.0 if wb.source_cct_kelvin
        else abs(wb.estimated_temp_offset) > 0.08
    )
    wb_uncertainty = 0.25 if large_cast and wb.gray_world_confidence < 0.60 else 0.0
    risk = min(1.0, face_risk + saturation_risk + wb_uncertainty + min(0.25, clipping * 0.25))
    reasons: list[str] = []

    if content_flags.get("preserve_intent"):
        action, strength = "preserve", 0.0
        reasons.append(content_flags.get("reason", "intentional dark or transition content should be preserved"))
    elif content_flags.get("manual_review"):
        action, strength = "manual_review", 0.0
        reasons.append(content_flags.get("reason", "special content needs review before automatic grading"))
    elif technical_need < 0.16 and risk < 0.25:
        action, strength = "preserve", 0.0
        reasons.append("source is already technically balanced; identity is safer than an unnecessary grade")
    elif risk >= 0.72:
        action, strength = "manual_review", 0.0
        reasons.append("creative transform risk is high relative to the measurable benefit")
    elif technical_need >= 0.45:
        action, strength = "technical_only", 0.0
        reasons.append("technical correction is useful, but creative styling should wait until the image is normalized")
    elif face.face_count or risk >= 0.30:
        action, strength = "creative_low", 0.45
        reasons.append("people or color-risk regions are present; use a restrained creative transform")
    else:
        action, strength = "creative_full", 1.0
        reasons.append("the shot is technically stable and has enough safety margin for a creative transform")
    confidence = min(0.95, 0.60 + abs(technical_need - risk) * 0.30 + wb.candidate_agreement * 0.10)
    if content_flags.get("preserve_intent"):
        confidence = max(confidence, 0.90)
    return GradeDecision(
        action=action, technical_need=round(technical_need, 4), creative_risk=round(risk, 4),
        confidence=round(confidence, 4), recommended_look_strength=strength, reasons=reasons,
    )

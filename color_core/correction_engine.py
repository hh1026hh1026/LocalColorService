"""
Correction Engine System for Local Color Service
Calculates conservative auto-correction advice and builds validated GradeRecipe objects.

V0.6.2 changes two things about exposure:

* The **magnitude** is computed in scene-linear light. Moving a shot from a
  display median of 0.20 to 0.40 is two stops, not the 0.4 EV the previous
  display-domain interpolation produced. The old figure was not a conservative
  choice, it was an arithmetic accident.
* When a face is present, exposure is anchored on **skin luminance** rather than
  the whole-frame median. A frame-wide median is dominated by sky, dark
  backgrounds and letterbox bars, none of which should decide how bright a
  person is.

The skin anchor uses a tolerance band rather than a single target. Pinning every
face to one luminance would brighten deep skin tones and darken pale ones toward
a common value, which is both photographically wrong and unfair to the subject.
"""

import math
from typing import Dict, Any, List, Optional

import numpy as np
from pydantic import BaseModel, Field
from color_core.image_analyzer import ImageAnalysisReport, decode_rec709
from color_core.face_analysis import FaceAnalysis
from color_core.recipe import GradeRecipe

# Display-domain luminance band a face is considered correctly exposed in,
# roughly 45-72 IRE. Anything inside is left alone whatever the frame median says.
SKIN_TARGET_BAND = (0.45, 0.72)

# A face must occupy at least this share of frame before it is trusted to drive
# exposure for the whole shot.
MIN_FACE_AREA_FOR_ANCHOR = 0.015

# Mired offset from D65 beyond which a colour cast is worth correcting. 10 mired
# is about half of a 1/8 CTO gel; below that, correction is not distinguishable
# from estimator noise.
MIN_CAST_MIRED = 10.0

EXPOSURE_CLAMP_EV = 0.8

# Highlight clipping the automatic path is allowed to introduce. A source shot
# that already clips more than this keeps its own level as the budget - the
# grade should not be blamed for damage that was in the camera original.
MAX_INTRODUCED_HIGHLIGHT_CLIPPING = 0.02

# The percentile model is very slightly optimistic against measurement (it
# predicted 3% clipping at +0.46 EV where the baked LUT produced 3.9% at
# +0.40 EV), so the derived ceiling is pulled back by this margin.
HEADROOM_SAFETY_EV = 0.10


def exposure_headroom_ev(report: ImageAnalysisReport, target_clipping: float | None = None) -> float:
    """Largest positive EV that keeps highlight clipping within budget.

    A linear gain of ``g = 2**EV`` maps a scene-linear luminance ``v`` to
    ``g*v``, so everything above ``1/g`` clips. Inverting that against the
    measured luminance distribution gives the ceiling directly.

    This exists because V0.6.2's move to true linear-light EV made the automatic
    exposure far stronger - and nothing on that path protected highlights. On a
    136-shot project the source had *zero* shots above the 6% clipping threshold
    (worst 2.47%) while the graded output had ten, one of them at 21.7%.
    """
    budget = MAX_INTRODUCED_HIGHLIGHT_CLIPPING if target_clipping is None else float(target_clipping)
    source_clipping = float(report.clipping.highlight_clipping_ratio)
    budget = max(budget, source_clipping)

    p95 = float(report.luminance.linear_p95)
    p99 = float(report.luminance.linear_p99)
    if p95 <= 0.0:
        return EXPOSURE_CLAMP_EV
    if p99 <= 0.0:
        p99 = p95
    # Log-space interpolation between the two known percentiles for the
    # (1 - budget) percentile that must stay below 1.0 after the lift.
    target_percentile = min(99.0, max(95.0, 100.0 * (1.0 - budget)))
    weight = (target_percentile - 95.0) / 4.0
    ceiling_value = float(
        np.exp(np.log(max(p95, 1e-6)) + weight * (np.log(max(p99, 1e-6)) - np.log(max(p95, 1e-6))))
    )
    headroom = math.log2(1.0 / max(ceiling_value, 1e-6)) - HEADROOM_SAFETY_EV
    return float(max(0.0, min(EXPOSURE_CLAMP_EV, headroom)))


class AutoCorrectionAdvice(BaseModel):
    suggested_recipe: GradeRecipe
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score of recommendation")
    rationales: List[str] = Field(default_factory=list, description="Reasoning behind auto adjustments")
    applied_safety_bounds: Dict[str, str] = Field(default_factory=dict)
    # What the measurement actually asked for, before the safety clamp. When it
    # differs materially from the applied value, the shot needs a human.
    requested_exposure_ev: float = 0.0
    exposure_anchor: str = "frame_median"
    # Set when the lift the subject needed was cut short to protect highlights.
    # These shots are the ones that genuinely need a local (secondary) fix.
    highlight_limited: bool = False
    exposure_headroom_ev: float = 0.0


def _linear_luminance(rgb: List[float]) -> float:
    """Rec.709 luminance of a display-encoded RGB triplet, in linear light."""
    linear = decode_rec709(rgb)
    return float(0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2])


def _exposure_from_skin(face: Optional[FaceAnalysis]):
    """Exposure in EV needed to bring skin into the target band, or None."""
    if face is None or not face.face_count or face.face_area_ratio < MIN_FACE_AREA_FOR_ANCHOR:
        return None
    if not face.mean_skin_rgb or face.mean_skin_luminance is None:
        return None
    current_display = float(face.mean_skin_luminance)
    low, high = SKIN_TARGET_BAND
    if low <= current_display <= high:
        return (
            0.0,
            "skin_anchor",
            f"Skin sits at {current_display:.2f} display luminance, inside the "
            f"[{low:.2f}, {high:.2f}] band. Exposure untouched.",
        )
    target_display = low if current_display < low else high
    current_linear = max(_linear_luminance(face.mean_skin_rgb), 1e-4)
    target_linear = max(float(decode_rec709(target_display)), 1e-4)
    ev = math.log2(target_linear / current_linear)
    return (
        ev,
        "skin_anchor",
        f"Skin at {current_display:.2f} display luminance is outside the "
        f"[{low:.2f}, {high:.2f}] band; {ev:+.2f} EV brings it to the nearest edge.",
    )


def _limited_gains(wb) -> tuple[float, float, float]:
    """Estimated gains, with the white-point shift bounded in mired."""
    from color_core.white_balance import (
        cct_duv_from_white,
        gains_from_source_white,
        limit_white_point_shift,
        source_white_from_gains,
        white_from_cct_duv,
    )

    raw = (float(wb.gain_r), float(wb.gain_g), float(wb.gain_b))
    try:
        cct, duv = cct_duv_from_white(source_white_from_gains(raw))
        source_mired = 1e6 / max(cct, 1.0)
        # The correction aims at D65; how far it may travel is what is bounded.
        target = limit_white_point_shift(source_mired, 1e6 / 6503.5)
        limited = gains_from_source_white(white_from_cct_duv(1e6 / max(target, 1e-6), duv))
        return (float(limited[0]), float(limited[1]), float(limited[2]))
    except Exception:
        return tuple(float(np_clip(value, 0.70, 1.45)) for value in raw)  # type: ignore[return-value]


def _exposure_from_frame(report: ImageAnalysisReport):
    """Fallback anchor: the frame median, with the magnitude taken in linear light."""
    display_median = report.luminance.median
    linear_median = report.luminance.linear_median or max(float(decode_rec709(display_median)), 1e-4)
    if display_median < 0.25:
        target_display = 0.40
    elif display_median > 0.60:
        target_display = 0.45
    else:
        return (
            0.0,
            "frame_median",
            f"Luminance median ({display_median:.2f}) is in neutral range. Exposure untouched.",
        )
    target_linear = max(float(decode_rec709(target_display)), 1e-4)
    ev = math.log2(target_linear / max(linear_median, 1e-4))
    direction = "Under-exposed" if ev > 0 else "Over-exposed"
    return (
        ev,
        "frame_median",
        f"{direction} input (median lum {display_median:.2f}); {ev:+.2f} EV targets {target_display:.2f}.",
    )


def generate_auto_correction_advice(
    report: ImageAnalysisReport,
    source_hash: str = "",
    target_lut_size: int = 33,
    face: Optional[FaceAnalysis] = None,
    content_flags: Optional[Dict[str, Any]] = None,
) -> AutoCorrectionAdvice:
    """
    Generates conservative color grading parameters based on image analysis statistics.
    Enforces strict safety bounds to prevent clipping or over-saturation.
    """
    rationales: List[str] = []
    safety_bounds: Dict[str, str] = {}
    content_flags = content_flags or {}

    gain_r = gain_g = gain_b = 1.0
    confidence_scores: List[float] = [0.85]  # Baseline confidence

    # 1. Exposure - skin first, frame median only as a fallback.
    if content_flags.get("preserve_intent"):
        requested_ev, anchor = 0.0, "preserve"
        rationales.append(
            content_flags.get("reason", "intentional dark or transition content; exposure preserved")
        )
    else:
        skin = _exposure_from_skin(face)
        requested_ev, anchor, reason = skin if skin is not None else _exposure_from_frame(report)
        rationales.append(reason)

    exposure_adj = max(-EXPOSURE_CLAMP_EV, min(EXPOSURE_CLAMP_EV, requested_ev))

    # Highlight protection. Only lifts can blow highlights, so the ceiling is
    # applied to positive exposure alone.
    headroom = exposure_headroom_ev(report)
    highlight_limited = False
    if exposure_adj > headroom:
        highlight_limited = True
        rationales.append(
            f"Highlight headroom allows {headroom:+.2f} EV before clipping exceeds "
            f"{MAX_INTRODUCED_HIGHLIGHT_CLIPPING:.0%}; requested {exposure_adj:+.2f} EV was reduced. "
            "Lifting the subject further needs a local correction, not a global one."
        )
        exposure_adj = headroom
        confidence_scores.append(0.50)

    # A lift that reaches its ceiling gets a softened shoulder. Measured, this
    # only buys back a fraction of the clipping (3.9% -> 2.4% at +0.4 EV, far
    # less at +0.8), so it is a finishing touch on top of the cap, never a
    # substitute for it.
    highlight_softness = 0.0
    if highlight_limited:
        # At the ceiling the shot has little or no headroom left, so the
        # shoulder is softened regardless of how small the surviving lift is.
        highlight_softness = 0.45
    elif exposure_adj > 0.05:
        highlight_softness = float(min(0.6, exposure_adj / EXPOSURE_CLAMP_EV * 0.6))

    if abs(requested_ev - exposure_adj) > 0.05:
        safety_bounds["exposure"] = (
            f"Clamped to +/-{EXPOSURE_CLAMP_EV} EV, then limited by highlight headroom"
            if highlight_limited else f"Clamped to +/-{EXPOSURE_CLAMP_EV} EV safe range"
        )
        rationales.append(
            f"Measurement asked for {requested_ev:+.2f} EV; only {exposure_adj:+.2f} EV was applied. "
            "A shot needing more than the automatic envelope should be reviewed."
        )
        confidence_scores.append(0.45)

    # 2. White Balance - the estimate itself is made in linear light upstream.
    wb = report.white_balance
    if wb.source_cct_kelvin:
        significant_cast = abs(wb.mired_offset_from_d65) > MIN_CAST_MIRED
    else:
        significant_cast = abs(wb.estimated_temp_offset) > 0.05
    if wb.gray_world_confidence > 0.60 and significant_cast:
        # Bound the correction in mired, not by clamping gains.
        #
        # A per-channel clamp saturates: target white points of 210, 230 and 260
        # mired all collapsed onto exactly 198.0 under the old [0.80, 1.25]
        # envelope, and it was asymmetric (-29/+43 around D65). Two shots either
        # side of the envelope therefore landed far apart, so the limiter itself
        # created discontinuities at cuts. This is the same limiter the
        # scene-group path uses, so both routes now agree.
        gain_r, gain_g, gain_b = _limited_gains(wb)
        if wb.source_cct_kelvin:
            rationales.append(
                f"Estimated illuminant {wb.source_cct_kelvin:.0f}K "
                f"({wb.mired_offset_from_d65:+.0f} mired from D65, Duv {wb.source_duv:+.4f}); "
                f"applied gains [R:{gain_r:.2f}, G:{gain_g:.2f}, B:{gain_b:.2f}]."
            )
        else:
            rationales.append(
                f"Color cast detected. Applied WB gains [R:{gain_r:.2f}, G:{gain_g:.2f}, B:{gain_b:.2f}]."
            )
        confidence_scores.append(wb.gray_world_confidence)
    else:
        rationales.append("White balance appears balanced or the scene tint is intentional.")

    # Linear-light gains sit further from unity than the display-domain gains
    # this bound was originally written for, so the envelope is widened to match.
    safety_bounds["rgb_gains"] = "White-point shift limited to +/-40 mired (see white_balance.limit_white_point_shift)"

    # 3. Contrast Auto-Correction
    rng = report.contrast.range_p05_p95
    if rng < 0.40:
        contrast_adj = 1.10
        rationales.append(f"Flat dynamic range (P95-P05 = {rng:.2f}). Applied +10% contrast boost.")
    elif rng > 0.85:
        contrast_adj = 0.95
        rationales.append(f"High dynamic range (P95-P05 = {rng:.2f}). Reduced contrast slightly to protect highlights.")
    else:
        contrast_adj = 1.0
        rationales.append(f"Contrast range ({rng:.2f}) is optimal.")

    contrast_adj = max(0.85, min(1.20, contrast_adj))
    safety_bounds["contrast"] = "Clamped to [0.85, 1.20] safe range"

    # 4. Saturation Auto-Correction
    sat_mean = report.saturation.mean
    if sat_mean < 0.15 and "monochrome" not in " ".join(report.anomalies).lower():
        sat_adj = 1.08
        rationales.append(f"Slightly muted colors (mean sat {sat_mean:.2f}). Applied a conservative +8% saturation boost.")
    else:
        sat_adj = 1.0
        if sat_mean > 0.45:
            rationales.append(f"Colorful source (mean sat {sat_mean:.2f}). Preserved source saturation.")

    # Positive exposure and display rendering can reduce perceived chroma. Compensate gently.
    if exposure_adj > 0.0:
        compensation = min(0.08, exposure_adj * 0.20)
        sat_adj += compensation
        rationales.append(f"Added {compensation * 100:.1f}% chroma compensation for the exposure lift.")

    sat_adj = max(0.85, min(1.15, sat_adj))
    safety_bounds["saturation"] = "Clamped to [0.85, 1.15] safe range"

    final_confidence = round(float(sum(confidence_scores) / len(confidence_scores)), 2)

    recipe = GradeRecipe(
        exposure=round(exposure_adj, 3),
        rgb_gains=[round(gain_r, 3), round(gain_g, 3), round(gain_b, 3)],
        contrast=round(contrast_adj, 3),
        saturation=round(sat_adj, 3),
        highlight_rolloff=round(min(1.0, highlight_softness * (0.90 if highlight_limited else 0.70)), 3),
        highlight_softness=round(highlight_softness, 3),
        pivot=0.18,
        lut_size=target_lut_size,
        strength=1.0,
        source_media_hash=source_hash,
        confidence=final_confidence,
        rationales=rationales,
        safety_bounds=safety_bounds,
        parameter_sources={
            "exposure": "skin_or_frame_analysis",
            "rgb_gains": "white_balance_analysis" if significant_cast else "neutral_default",
            "contrast": "dynamic_range_analysis",
            "saturation": "chroma_analysis",
            "pivot": "safe_default",
            "highlight_rolloff": "derived_from_headroom",
            "highlight_softness": "highlight_headroom_analysis",
            "temperature": "white_balance_analysis" if significant_cast else "neutral_default",
            "tint": "neutral_default",
            "color_density": "neutral_default",
            "skin_protection": "look_package_or_quality_profile",
            "gamut_protection": "look_package_or_quality_profile",
            "lift": "neutral_default",
            "gamma": "neutral_default",
            "gain": "neutral_default",
        },
        parameter_confidence={
            "exposure": round(final_confidence, 2),
            "rgb_gains": round(float(wb.gray_world_confidence), 2) if significant_cast else 0.70,
            "contrast": 0.82,
            "saturation": 0.78,
            "pivot": 0.60,
            "highlight_rolloff": 0.76,
            "highlight_softness": 0.82 if highlight_limited else 0.68,
            "temperature": round(float(wb.gray_world_confidence), 2) if significant_cast else 0.70,
            "tint": 0.45,
            "color_density": 0.35,
            "skin_protection": 0.80 if face and face.face_count else 0.40,
            "gamut_protection": 0.80,
            "lift": 0.25,
            "gamma": 0.25,
            "gain": 0.25,
        },
        exposure_diagnostics={
            "anchor": anchor,
            "requested_ev": round(float(requested_ev), 4),
            "applied_ev": round(exposure_adj, 4),
            "headroom_ev": round(float(headroom), 4),
            "highlight_limited": bool(highlight_limited),
            "skin_luminance": (
                round(float(face.mean_skin_luminance), 4)
                if face is not None and face.mean_skin_luminance is not None else None
            ),
        },
    )

    return AutoCorrectionAdvice(
        suggested_recipe=recipe,
        confidence=final_confidence,
        rationales=rationales,
        applied_safety_bounds=safety_bounds,
        requested_exposure_ev=round(float(requested_ev), 4),
        exposure_anchor=anchor,
        highlight_limited=highlight_limited,
        exposure_headroom_ev=round(float(headroom), 4),
    )


def np_clip(val: float, low: float, high: float) -> float:
    return max(low, min(high, float(val)))

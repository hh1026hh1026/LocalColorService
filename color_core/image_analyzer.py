"""
Image Analyzer System for Local Color Service
Calculates comprehensive color statistical metrics (luminance, clipping, channel statistics, white balance, contrast, saturation).
"""

import cv2
import numpy as np
import colour
from typing import List, Dict, Any
from pydantic import BaseModel, Field


class LuminanceMetrics(BaseModel):
    # Display-encoded (Rec.709 OETF) luma. Retained because continuity and
    # jump thresholds are perceptual comparisons and are calibrated on it.
    mean: float
    median: float
    p01: float = 0.0
    p05: float
    p50: float
    p95: float
    p99: float = 1.0
    # Scene-linear luminance. Anything photometric - exposure in EV, white
    # balance - must use these, not the encoded values above.
    linear_mean: float = 0.0
    linear_median: float = 0.0
    linear_p05: float = 0.0
    linear_p95: float = 0.0
    linear_p99: float = 0.0


class ClippingMetrics(BaseModel):
    black_clipping_ratio: float  # Lum < 1% (2.55/255)
    highlight_clipping_ratio: float  # Lum > 99% (252.45/255)
    red_highlight_clipping_ratio: float = 0.0
    green_highlight_clipping_ratio: float = 0.0
    blue_highlight_clipping_ratio: float = 0.0


class ChannelMetrics(BaseModel):
    r_mean: float
    g_mean: float
    b_mean: float
    r_median: float
    g_median: float
    b_median: float


class WhiteBalanceMetrics(BaseModel):
    gain_r: float
    gain_g: float
    gain_b: float
    estimated_temp_offset: float  # Negative = cool, Positive = warm
    gray_world_confidence: float
    shades_of_gray_gains: list[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    neutral_highlight_gains: list[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    candidate_agreement: float = 0.0
    selected_method: str = "gray_world"
    # Physical description of the estimated illuminant. `estimated_temp_offset`
    # above is a bare gain difference with no unit; these are the quantities
    # scene grouping, shot matching and continuity QC should compare on.
    source_cct_kelvin: float = 0.0
    source_duv: float = 0.0
    mired_offset_from_d65: float = 0.0


class SaturationMetrics(BaseModel):
    mean: float
    median: float
    p95: float
    std: float


class ContrastMetrics(BaseModel):
    range_p05_p95: float
    rms_contrast: float


class ImageAnalysisReport(BaseModel):
    luminance: LuminanceMetrics
    clipping: ClippingMetrics
    channel: ChannelMetrics
    white_balance: WhiteBalanceMetrics
    saturation: SaturationMetrics
    contrast: ContrastMetrics
    anomalies: List[str] = Field(default_factory=list)


def decode_rec709(encoded: np.ndarray) -> np.ndarray:
    """Rec.709 display-encoded values to scene-linear.

    Gray-world and its variants assume the *average scene reflectance* is
    neutral. That is a statement about linear light. Averaging channels in the
    encoded domain over-weights the shadows (the OETF expands them) and
    systematically understates the correction, which is why the encoded-domain
    estimate had to be raised to a power before it could be used as a physical
    gain.
    """
    value = np.clip(np.asarray(encoded, dtype=np.float32), 0.0, 1.0)
    return np.where(value <= 0.081, value / 4.5, ((value + 0.099) / 1.099) ** (1.0 / 0.45))


SKIN_EXCLUSION_MIN_REMAINING = 0.15


def _white_balance_selector(frame: np.ndarray, base: np.ndarray | None) -> np.ndarray | None:
    """Pixel selector for the white-balance statistics, with skin removed.

    Gray-world assumes the scene averages to neutral. A frame filled with a face
    does not: skin is orange, so the estimator reads the subject as the light and
    corrects the picture cool. Measured on the golden set, a half-skin frame
    under D65 was estimated at 5247K (36.8 mired out); removing skin from the
    statistics brought it to exactly 6504K.

    Skin is only removed while enough of the frame survives to estimate from.
    A true close-up keeps all its pixels and instead reports low agreement, which
    is the signal scene-group consensus needs in order to step in.

    Crucially this only runs when a face is actually *detected*. The YCrCb skin
    classifier on its own is a broad colour range, not a face detector: measured
    on synthetic scenes containing no people at all it selected between 16% and
    90% of pixels (wood, earth and warm surfaces all qualify). Excluding those
    destroyed the neutral average the estimator depends on and turned a 0.1 mired
    result into 61.7. Detection is what makes the exclusion meaningful.
    """
    try:
        from color_core.face_analysis import skin_mask_bgr

        _, boxes = skin_mask_bgr(frame, face_only=True)
        if not boxes:
            return base
        mask, _ = skin_mask_bgr(frame, face_only=False)
    except Exception:
        return base
    keep = (mask < 64).ravel()
    if base is not None:
        keep = keep & base
    if float(np.mean(keep)) < SKIN_EXCLUSION_MIN_REMAINING:
        return base
    return keep


def analyze_image_frames(
    frames: List[np.ndarray],
    weights: List[np.ndarray] | None = None,
    exclude_skin_from_white_balance: bool = True,
) -> ImageAnalysisReport:
    """
    Analyzes sampled frames (BGR uint8 format) and aggregates statistical metrics.

    ``weights`` optionally supplies a per-frame spatial weight map in [0, 1]
    (same height/width as the frame). Pixels with zero weight are excluded
    entirely, which is how letterbox bars are kept out of the statistics.
    """
    if not frames:
        raise ValueError("No image frames provided for analysis.")

    lums: List[np.ndarray] = []
    linear_lums: List[np.ndarray] = []
    b_channels: List[np.ndarray] = []
    g_channels: List[np.ndarray] = []
    r_channels: List[np.ndarray] = []
    linear_channels: List[List[np.ndarray]] = [[], [], []]
    wb_channels: List[List[np.ndarray]] = [[], [], []]
    sats: List[np.ndarray] = []

    anomalies: List[str] = []
    # Colour Science supplies the active ITU-R BT.709 RGB-to-XYZ matrix.
    y_weights = colour.RGB_COLOURSPACES["ITU-R BT.709"].matrix_RGB_to_XYZ[1]

    for index, img in enumerate(frames):
        # Convert BGR (uint8) to float [0, 1]
        img_f = img.astype(np.float32) / 255.0
        b, g, r = img_f[:, :, 0], img_f[:, :, 1], img_f[:, :, 2]

        selector = None
        if weights is not None and index < len(weights) and weights[index] is not None:
            weight = np.asarray(weights[index], dtype=np.float32)
            if weight.shape == b.shape:
                selector = weight.ravel() > 0.0

        def flat(channel: np.ndarray) -> np.ndarray:
            raveled = channel.ravel()
            return raveled[selector] if selector is not None else raveled

        lum = y_weights[0] * r + y_weights[1] * g + y_weights[2] * b
        lums.append(flat(lum))

        linear_r, linear_g, linear_b = decode_rec709(r), decode_rec709(g), decode_rec709(b)
        linear_lums.append(
            flat(y_weights[0] * linear_r + y_weights[1] * linear_g + y_weights[2] * linear_b)
        )
        # White balance gets its own selector so skin can be excluded from the
        # illuminant estimate without affecting exposure or clipping statistics,
        # which legitimately need the subject.
        wb_selector = (
            _white_balance_selector(img, selector)
            if exclude_skin_from_white_balance else selector
        )
        for slot, channel in enumerate((linear_r, linear_g, linear_b)):
            linear_channels[slot].append(flat(channel))
            raveled = channel.ravel()
            wb_channels[slot].append(raveled[wb_selector] if wb_selector is not None else raveled)

        b_channels.append(flat(b))
        g_channels.append(flat(g))
        r_channels.append(flat(r))

        # Saturation in HSV color space
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32) / 255.0
        sats.append(flat(hsv[:, :, 1]))

    # Concatenate all sampled pixels
    all_lum = np.concatenate(lums)
    all_linear_lum = np.concatenate(linear_lums)
    all_b = np.concatenate(b_channels)
    all_g = np.concatenate(g_channels)
    all_r = np.concatenate(r_channels)
    all_sat = np.concatenate(sats)
    linear_r_all = np.concatenate(linear_channels[0])
    linear_g_all = np.concatenate(linear_channels[1])
    linear_b_all = np.concatenate(linear_channels[2])
    wb_r_all = np.concatenate(wb_channels[0]) if wb_channels[0] else linear_r_all
    wb_g_all = np.concatenate(wb_channels[1]) if wb_channels[1] else linear_g_all
    wb_b_all = np.concatenate(wb_channels[2]) if wb_channels[2] else linear_b_all
    if wb_r_all.size < 256:
        wb_r_all, wb_g_all, wb_b_all = linear_r_all, linear_g_all, linear_b_all
    if all_lum.size == 0:
        raise ValueError("Spatial weights excluded every pixel from analysis.")

    # 1. Luminance Metrics
    lum_mean = float(np.mean(all_lum))
    lum_median = float(np.median(all_lum))
    p05 = float(np.percentile(all_lum, 5))
    p01 = float(np.percentile(all_lum, 1))
    p50 = float(np.percentile(all_lum, 50))
    p95 = float(np.percentile(all_lum, 95))
    p99 = float(np.percentile(all_lum, 99))

    # 2. Clipping Metrics
    black_clip_mask = all_lum < 0.01
    high_clip_mask = all_lum > 0.99
    black_clip_ratio = float(np.mean(black_clip_mask))
    high_clip_ratio = float(np.mean(high_clip_mask))
    red_high_clip = float(np.mean(all_r > 0.99))
    green_high_clip = float(np.mean(all_g > 0.99))
    blue_high_clip = float(np.mean(all_b > 0.99))

    if black_clip_ratio > 0.05:
        anomalies.append(f"Severe black clipping detected ({black_clip_ratio * 100:.1f}% pixels crushed)")
    if high_clip_ratio > 0.03:
        anomalies.append(f"Highlight clipping detected ({high_clip_ratio * 100:.1f}% pixels blown out)")

    # 3. Channel Metrics
    r_mean = float(np.mean(all_r))
    g_mean = float(np.mean(all_g))
    b_mean = float(np.mean(all_b))
    r_median = float(np.median(all_r))
    g_median = float(np.median(all_g))
    b_median = float(np.median(all_b))

    # 4. White Balance Estimation - performed in SCENE-LINEAR light.
    # Gray-world and its variants are statements about average scene
    # reflectance, which is a linear-light quantity. Estimating them on
    # display-encoded values weights shadows far too heavily and understates
    # the correction.
    linear_r_mean = float(np.mean(wb_r_all))
    linear_g_mean = float(np.mean(wb_g_all))
    linear_b_mean = float(np.mean(wb_b_all))
    avg_rgb = (linear_r_mean + linear_g_mean + linear_b_mean) / 3.0
    gain_r = float(avg_rgb / max(linear_r_mean, 1e-6))
    gain_g = float(avg_rgb / max(linear_g_mean, 1e-6))
    gain_b = float(avg_rgb / max(linear_b_mean, 1e-6))

    # Normalize gain_g to 1.0.
    if gain_g > 0:
        gain_r /= gain_g
        gain_b /= gain_g
        gain_g = 1.0

    # Shades-of-Gray uses a Minkowski norm and is less dominated by large
    # uniform areas than plain Gray World.
    power = 6.0
    shades_channels = [
        float(np.mean(np.power(np.clip(channel, 0.0, 1.0), power)) ** (1.0 / power))
        for channel in (wb_r_all, wb_g_all, wb_b_all)
    ]
    shades_target = sum(shades_channels) / 3.0
    shades = [shades_target / max(value, 1e-6) for value in shades_channels]
    shades = [value / max(shades[1], 1e-6) for value in shades]

    # Near-neutral highlights provide a third candidate. A low sample count
    # intentionally lowers confidence instead of forcing a correction.
    wb_lum = 0.2126 * wb_r_all + 0.7152 * wb_g_all + 0.0722 * wb_b_all
    highlight_floor = float(np.percentile(wb_lum, 75))
    neutral_mask = (wb_lum >= highlight_floor) & (wb_lum <= float(np.percentile(wb_lum, 99)))
    neutral_ratio = float(np.mean(neutral_mask))
    if int(np.count_nonzero(neutral_mask)) >= 256:
        neutral_channels = [
            float(np.mean(channel[neutral_mask]))
            for channel in (wb_r_all, wb_g_all, wb_b_all)
        ]
        neutral_target = sum(neutral_channels) / 3.0
        neutral = [neutral_target / max(value, 1e-6) for value in neutral_channels]
        neutral = [value / max(neutral[1], 1e-6) for value in neutral]
    else:
        neutral = [gain_r, gain_g, gain_b]

    # Combine the candidates in MIRED/DUV, not in gain units.
    #
    # Gain-space disagreement scales with how far the illuminant is from D65,
    # because a strong cast needs gains far from 1.0 and the same relative
    # disagreement becomes a larger absolute number. Measured on the golden set,
    # the candidate spread in mired is 13-21 for every illuminant from 3200K to
    # 9000K, while in gain units the same scenes read 0.077 to 0.228 - the
    # estimator was being penalised for the light being warm, not for being
    # uncertain. A 9000K scene it could solve to 0.0 mired was discarded.
    candidates = [
        (float(gain_r), float(gain_g), float(gain_b)),
        (float(shades[0]), float(shades[1]), float(shades[2])),
        (float(neutral[0]), float(neutral[1]), float(neutral[2])),
    ]
    candidate_mireds: list[float] = []
    candidate_duvs: list[float] = []
    for candidate in candidates:
        try:
            from color_core.white_balance import cct_duv_from_white, source_white_from_gains

            cct, duv = cct_duv_from_white(source_white_from_gains(candidate))
            candidate_mireds.append(1e6 / max(cct, 1.0))
            candidate_duvs.append(duv)
        except Exception:
            continue

    if len(candidate_mireds) >= 2:
        spread = float(np.ptp(candidate_mireds))
        # 60 mired is about a third of a CTO. Fair scenes measured 1.3-12.5;
        # scenes that violate the gray-world assumption measured 19-45.
        agreement = float(np.clip(1.0 - spread / 60.0, 0.0, 1.0))
        try:
            from color_core.white_balance import gains_from_source_white, white_from_cct_duv

            consensus_mired = float(np.median(candidate_mireds))
            consensus_duv = float(np.median(candidate_duvs))
            gain_r, gain_g, gain_b = gains_from_source_white(
                white_from_cct_duv(1e6 / max(consensus_mired, 1e-6), consensus_duv)
            )
            # The estimate is always reported, even when confidence is low.
            # Discarding it (the old "preserve_low_confidence" path returned unit
            # gains) threw away the one thing scene-group consensus needs, and
            # made an unreadable shot indistinguishable from a truly neutral one.
            selected_method = "ensemble_mired" if agreement >= 0.50 else "ensemble_mired_low_confidence"
        except Exception:
            selected_method = "gray_world"
    else:
        spread = float(np.max(np.ptp(np.asarray(candidates, dtype=np.float64), axis=0)))
        agreement = float(np.clip(1.0 - spread / 0.44, 0.0, 1.0))
        selected_method = "gray_world"

    # Temperature offset estimation: (Blue gain - Red gain). Unitless, retained
    # for compatibility; prefer mired_offset_from_d65 below.
    temp_offset = float(gain_b - gain_r)
    wb_confidence = float(np.clip(0.35 + 0.5 * agreement + min(0.1, neutral_ratio), 0.0, 0.95))
    try:
        from color_core.white_balance import cct_duv_from_white, source_white_from_gains

        source_cct, source_duv = cct_duv_from_white(
            source_white_from_gains((gain_r, gain_g, gain_b))
        )
        mired_offset = 1e6 / max(source_cct, 1.0) - 1e6 / 6503.5
    except Exception:  # pragma: no cover - degenerate estimates only
        source_cct, source_duv, mired_offset = 0.0, 0.0, 0.0

    if abs(r_mean - b_mean) > 0.15:
        anomalies.append(f"Strong color cast detected (R_mean={r_mean:.2f}, B_mean={b_mean:.2f})")

    # 5. Saturation Metrics
    sat_mean = float(np.mean(all_sat))
    sat_median = float(np.median(all_sat))
    sat_p95 = float(np.percentile(all_sat, 95))
    sat_std = float(np.std(all_sat))

    if sat_mean < 0.10:
        anomalies.append("Desaturated or monochrome input detected")
    elif sat_p95 > 0.90:
        anomalies.append("High saturation overflow risk")

    # 6. Contrast Metrics
    range_contrast = float(p95 - p05)
    rms_contrast = float(np.std(all_lum))

    if range_contrast < 0.30:
        anomalies.append(f"Low dynamic contrast detected (P95-P05 = {range_contrast:.2f})")

    return ImageAnalysisReport(
        luminance=LuminanceMetrics(
            mean=lum_mean,
            median=lum_median,
            p01=p01,
            p05=p05,
            p50=p50,
            p95=p95,
            p99=p99,
            linear_mean=float(np.mean(all_linear_lum)),
            linear_median=float(np.median(all_linear_lum)),
            linear_p05=float(np.percentile(all_linear_lum, 5)),
            linear_p95=float(np.percentile(all_linear_lum, 95)),
            linear_p99=float(np.percentile(all_linear_lum, 99)),
        ),
        clipping=ClippingMetrics(
            black_clipping_ratio=black_clip_ratio,
            highlight_clipping_ratio=high_clip_ratio,
            red_highlight_clipping_ratio=red_high_clip,
            green_highlight_clipping_ratio=green_high_clip,
            blue_highlight_clipping_ratio=blue_high_clip,
        ),
        channel=ChannelMetrics(
            r_mean=r_mean,
            g_mean=g_mean,
            b_mean=b_mean,
            r_median=r_median,
            g_median=g_median,
            b_median=b_median
        ),
        white_balance=WhiteBalanceMetrics(
            gain_r=gain_r,
            gain_g=gain_g,
            gain_b=gain_b,
            estimated_temp_offset=temp_offset,
            gray_world_confidence=wb_confidence,
            shades_of_gray_gains=[float(value) for value in shades],
            neutral_highlight_gains=[float(value) for value in neutral],
            candidate_agreement=agreement,
            selected_method=selected_method,
            source_cct_kelvin=round(float(source_cct), 1),
            source_duv=round(float(source_duv), 6),
            mired_offset_from_d65=round(float(mired_offset), 3),
        ),
        saturation=SaturationMetrics(
            mean=sat_mean,
            median=sat_median,
            p95=sat_p95,
            std=sat_std
        ),
        contrast=ContrastMetrics(
            range_p05_p95=range_contrast,
            rms_contrast=rms_contrast
        ),
        anomalies=anomalies
    )

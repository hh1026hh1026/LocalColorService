"""
Colorimetry Metrics & Precision Evaluation Engine for Local Color Service V0.1.1.
Calculates PSNR, SSIM, Max RGB Error, Delta E 76, Gray Ramp Linearity, and Black/White Point Shifts.
"""

import cv2
import numpy as np
from typing import Dict, Any


def compute_psnr(img1: np.ndarray, img2: np.ndarray) -> float:
    """Computes Peak Signal-to-Noise Ratio (PSNR) in dB."""
    mse = np.mean((img1.astype(np.float64) - img2.astype(np.float64)) ** 2)
    if mse == 0:
        return 100.0  # Perfect match
    max_pixel = 255.0
    return float(20 * np.log10(max_pixel / np.sqrt(mse)))


def compute_max_rgb_error(img1: np.ndarray, img2: np.ndarray) -> float:
    """Computes maximum absolute RGB pixel error (0-255 scale)."""
    diff = np.abs(img1.astype(np.float32) - img2.astype(np.float32))
    return float(np.max(diff))


def compute_delta_e76(img1_bgr: np.ndarray, img2_bgr: np.ndarray) -> Dict[str, float]:
    """
    Computes CIE1976 Delta E color difference in Lab space.
    """
    lab1 = cv2.cvtColor(img1_bgr, cv2.COLOR_BGR2Lab).astype(np.float32)
    lab2 = cv2.cvtColor(img2_bgr, cv2.COLOR_BGR2Lab).astype(np.float32)

    # OpenCV Lab scale: L [0, 255], a [0, 255], b [0, 255] -> normalize to standard CIE Lab
    # L_std = L * 100/255, a_std = a - 128, b_std = b - 128
    l1, a1, b1 = lab1[:, :, 0] * 100.0 / 255.0, lab1[:, :, 1] - 128.0, lab1[:, :, 2] - 128.0
    l2, a2, b2 = lab2[:, :, 0] * 100.0 / 255.0, lab2[:, :, 1] - 128.0, lab2[:, :, 2] - 128.0

    delta_e = np.sqrt((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)

    return {
        "mean_delta_e": float(np.mean(delta_e)),
        "max_delta_e": float(np.max(delta_e)),
        "p95_delta_e": float(np.percentile(delta_e, 95))
    }


def rec709_to_lab(rgb: "np.ndarray | list[float]") -> np.ndarray:
    """Convert Rec.709-encoded RGB in [0,1] to CIE L*a*b* (D65).

    Used for skin-tone comparisons, where Delta E must be computed in a real
    colour space rather than OpenCV's 8-bit Lab approximation.
    """
    import colour

    values = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    linear = colour.cctf_decoding(values, function="ITU-R BT.709")
    xyz = colour.RGB_to_XYZ(
        linear,
        colour.RGB_COLOURSPACES["ITU-R BT.709"],
        illuminant=colour.RGB_COLOURSPACES["ITU-R BT.709"].whitepoint,
        apply_cctf_decoding=False,
    )
    return np.asarray(colour.XYZ_to_Lab(xyz), dtype=np.float64)


def delta_e2000_rec709(rgb_a: "np.ndarray | list[float]", rgb_b: "np.ndarray | list[float]") -> float:
    """CIEDE2000 between two Rec.709-encoded RGB triplets.

    Delta E76 (used elsewhere in this module for bulk image comparison) badly
    underestimates differences in the orange region where skin tones live, so
    every skin-safety judgement uses CIEDE2000 instead.
    """
    import colour

    return float(
        colour.difference.delta_E_CIE2000(rec709_to_lab(rgb_a), rec709_to_lab(rgb_b))
    )


def skin_difference_rec709(
    rgb_source: "np.ndarray | list[float]", rgb_graded: "np.ndarray | list[float]"
) -> Dict[str, float]:
    """Decompose a skin-tone change into lightness, chroma and hue.

    Total Delta E is the wrong gate for skin safety. It contains Delta L*, so a
    deliberate exposure lift registers as a large "skin risk" - which is exactly
    what happened in production: across 38 flagged shots the correlation between
    Delta E2000 and the shot's |EV| was 0.813, and the twelve worst offenders
    were all shots that had hit the +0.80 EV ceiling. Making a face brighter is
    the point of the grade, not a defect.

    What a colourist actually watches is whether skin drifts toward magenta or
    green and whether it goes chalky or oversaturated. Both live in the
    chromatic plane, so ``chromatic`` - the hypotenuse of Delta C* and Delta H*,
    with lightness removed - is the number to judge on.

    Delta H* uses the CIE definition ``2*sqrt(C1*C2)*sin(dh/2)``, which keeps it
    in the same units as Delta E rather than degrees, so the two components are
    directly comparable.
    """
    import colour

    lab_source = rec709_to_lab(rgb_source)
    lab_graded = rec709_to_lab(rgb_graded)
    l1, a1, b1 = (float(value) for value in lab_source)
    l2, a2, b2 = (float(value) for value in lab_graded)

    chroma1 = float(np.hypot(a1, b1))
    chroma2 = float(np.hypot(a2, b2))
    hue1 = float(np.degrees(np.arctan2(b1, a1)) % 360.0)
    hue2 = float(np.degrees(np.arctan2(b2, a2)) % 360.0)
    hue_rotation = (hue2 - hue1 + 180.0) % 360.0 - 180.0

    delta_lightness = l2 - l1
    delta_chroma = chroma2 - chroma1
    delta_hue = 2.0 * float(np.sqrt(max(chroma1 * chroma2, 0.0))) * float(
        np.sin(np.radians(hue_rotation) / 2.0)
    )
    return {
        "delta_e2000": round(float(colour.difference.delta_E_CIE2000(lab_source, lab_graded)), 3),
        "delta_lightness": round(delta_lightness, 3),
        "delta_chroma": round(delta_chroma, 3),
        "delta_hue": round(delta_hue, 3),
        "hue_rotation_deg": round(hue_rotation, 2),
        # Lightness removed: this is the gate.
        "chromatic": round(float(np.hypot(delta_chroma, delta_hue)), 3),
    }


def evaluate_roundtrip_precision(original_img: np.ndarray, processed_img: np.ndarray) -> Dict[str, Any]:
    """
    Evaluates complete precision metrics between original image and processed image.
    """
    psnr_val = compute_psnr(original_img, processed_img)
    max_err = compute_max_rgb_error(original_img, processed_img)
    delta_e_stats = compute_delta_e76(original_img, processed_img)

    # Gray Ramp Linearity Check (Black level & White level shift)
    gray_orig = cv2.cvtColor(original_img, cv2.COLOR_BGR2GRAY)
    gray_proc = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)

    black_orig = float(np.percentile(gray_orig, 1))
    black_proc = float(np.percentile(gray_proc, 1))
    black_shift = abs(black_proc - black_orig)

    white_orig = float(np.percentile(gray_orig, 99))
    white_proc = float(np.percentile(gray_proc, 99))
    white_shift = abs(white_proc - white_orig)

    return {
        "psnr_db": round(psnr_val, 2),
        "max_rgb_error": round(max_err, 2),
        "mean_delta_e": round(delta_e_stats["mean_delta_e"], 4),
        "max_delta_e": round(delta_e_stats["max_delta_e"], 4),
        "p95_delta_e": round(delta_e_stats["p95_delta_e"], 4),
        "black_point_shift": round(black_shift, 2),
        "white_point_shift": round(white_shift, 2),
        "pass_neutral_roundtrip": psnr_val >= 42.0 and delta_e_stats["mean_delta_e"] < 0.50
    }

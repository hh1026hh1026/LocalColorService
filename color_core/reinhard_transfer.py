"""
Reinhard Color Transfer Algorithm System for Local Color Service V0.1.2.
Transfers color distribution statistics in CIE L*a*b* space from reference film stills/presets to target frames.
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Dict, Any, Tuple
from color_core.recipe import GradeRecipe


def reinhard_color_transfer(
    source_bgr: np.ndarray,
    reference_bgr: np.ndarray,
    blend_factor: float = 1.0
) -> np.ndarray:
    """
    Executes Reinhard L*a*b* statistical color transfer from reference image to source image.
    """
    src_lab = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2Lab).astype(np.float32)
    ref_lab = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2Lab).astype(np.float32)

    # Compute mean and standard deviation for source and reference
    src_mean, src_std = cv2.meanStdDev(src_lab)
    ref_mean, ref_std = cv2.meanStdDev(ref_lab)

    src_mean = src_mean.flatten()
    src_std = np.maximum(src_std.flatten(), 1e-5)
    ref_mean = ref_mean.flatten()
    ref_std = ref_std.flatten()

    # Shift and scale Lab channels
    res_lab = src_lab.copy()
    for c in range(3):
        scaled = (src_lab[:, :, c] - src_mean[c]) * (ref_std[c] / src_std[c]) + ref_mean[c]
        res_lab[:, :, c] = np.clip(scaled, 0.0, 255.0)

    # Convert back to BGR
    result_bgr = cv2.cvtColor(res_lab.astype(np.uint8), cv2.COLOR_Lab2BGR)

    if blend_factor < 1.0:
        blend_factor = max(0.0, min(1.0, blend_factor))
        result_bgr = cv2.addWeighted(source_bgr, 1.0 - blend_factor, result_bgr, blend_factor, 0)

    return result_bgr


def generate_recipe_from_reference(
    source_bgr: np.ndarray,
    reference_bgr: np.ndarray
) -> GradeRecipe:
    """
    Analyzes color statistics between source and reference image, and generates a GradeRecipe.
    """
    src_lab = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2Lab).astype(np.float32)
    ref_lab = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2Lab).astype(np.float32)

    src_mean, src_std = cv2.meanStdDev(src_lab)
    ref_mean, ref_std = cv2.meanStdDev(ref_lab)

    src_mean, src_std = src_mean.flatten(), src_std.flatten()
    ref_mean, ref_std = ref_mean.flatten(), ref_std.flatten()

    # Exposure delta (L channel ratio)
    lum_ratio = (ref_mean[0] + 1e-5) / (src_mean[0] + 1e-5)
    exposure_shift = float(np.clip(np.log2(lum_ratio) * 0.5, -1.0, 1.0))

    # Contrast ratio (L channel std ratio)
    contrast_ratio = float(np.clip(ref_std[0] / (src_std[0] + 1e-5), 0.88, 1.15))

    # Saturation ratio (a and b channel std ratio)
    sat_src_std = np.sqrt(src_std[1]**2 + src_std[2]**2) + 1e-5
    sat_ref_std = np.sqrt(ref_std[1]**2 + ref_std[2]**2) + 1e-5
    sat_ratio = float(np.clip(sat_ref_std / sat_src_std, 0.88, 1.12))

    # Color temperature delta (b channel shift)
    b_delta = float(ref_mean[2] - src_mean[2])
    temp_shift = float(np.clip(b_delta * 0.5, -15.0, 15.0))

    return GradeRecipe(
        exposure=round(exposure_shift, 2),
        contrast=round(contrast_ratio, 2),
        saturation=round(sat_ratio, 2),
        temperature=round(temp_shift, 1),
        filmic_s_curve=True,
        recommended=True,
        applied=False,
        confidence=0.88,
        rationales=[
            f"Reinhard Statistical Transfer: Exposure shift={exposure_shift:+.2f} EV",
            f"Contrast ratio={contrast_ratio:.2f}, Saturation ratio={sat_ratio:.2f}",
            f"Temperature offset={temp_shift:+.1f}° based on reference Lab distribution"
        ]
    )

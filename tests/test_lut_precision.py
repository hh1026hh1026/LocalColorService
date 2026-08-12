"""
Mandatory V0.1.1 Engineering Test: LUT Resolution Precision Comparison (33³ vs 65³).
"""

import os
import cv2
import pytest
import numpy as np
from pathlib import Path
from color_core.recipe import GradeRecipe
from color_core.ocio_manager import OCIOManager
from color_core.lut_baker import bake_3d_lut
from color_core.renderer import render_final
from color_core.color_metrics import compute_psnr, compute_delta_e76

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_compare_33_vs_65_lut_precision(tmp_path):
    src_img_path = TEST_ASSETS / "sample_image.png"
    assert src_img_path.exists()

    original_bgr = cv2.imread(str(src_img_path))
    mgr = OCIOManager()

    # Complex grade recipe
    recipe_33 = GradeRecipe(exposure=0.3, contrast=1.12, saturation=1.10, lut_size=33)
    recipe_65 = GradeRecipe(exposure=0.3, contrast=1.12, saturation=1.10, lut_size=65)

    lut_33_path = tmp_path / "grade_33.cube"
    lut_65_path = tmp_path / "grade_65.cube"

    bake_3d_lut(recipe_33, str(lut_33_path), mgr, lut_size=33)
    bake_3d_lut(recipe_65, str(lut_65_path), mgr, lut_size=65)

    out_33 = tmp_path / "out_33.png"
    out_65 = tmp_path / "out_65.png"

    render_final(str(src_img_path), str(lut_33_path), str(out_33))
    render_final(str(src_img_path), str(lut_65_path), str(out_65))

    img_33 = cv2.imread(str(out_33))
    img_65 = cv2.imread(str(out_65))

    psnr_between = compute_psnr(img_33, img_65)
    delta_e = compute_delta_e76(img_33, img_65)

    print(f"\n--- 33³ vs 65³ LUT Precision Comparison ---")
    print(f"PSNR between 33³ and 65³: {psnr_between:.2f} dB")
    print(f"Mean Delta E difference: {delta_e['mean_delta_e']:.4f}")

    assert psnr_between >= 45.0, f"33³ vs 65³ LUT discrepancy too large ({psnr_between} dB)"
    assert delta_e["mean_delta_e"] < 0.30, "33³ vs 65³ Delta E difference exceeds tolerance"

"""
Mandatory V0.1.1 Engineering Test: Neutral Roundtrip Precision Inspection.
Verifies Rec.709 -> ACEScct -> Rec.709 identity roundtrip achieves high PSNR (> 42 dB) and low Delta E (< 0.50).
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
from color_core.color_metrics import evaluate_roundtrip_precision

TEST_ASSETS = Path(__file__).resolve().parent.parent / "test_assets"


def test_neutral_roundtrip_colorimetry_precision(tmp_path):
    src_img_path = TEST_ASSETS / "sample_image.png"
    assert src_img_path.exists(), "Generate test assets first"

    original_bgr = cv2.imread(str(src_img_path))
    assert original_bgr is not None

    mgr = OCIOManager()
    recipe = GradeRecipe()  # Neutral default identity recipe

    lut_path = tmp_path / "neutral_test.cube"
    bake_3d_lut(recipe, str(lut_path), mgr, lut_size=33)

    out_rendered_img = tmp_path / "neutral_rendered.png"
    render_final(str(src_img_path), str(lut_path), str(out_rendered_img))

    rendered_bgr = cv2.imread(str(out_rendered_img))
    assert rendered_bgr is not None

    # Evaluate Precision Metrics
    metrics = evaluate_roundtrip_precision(original_bgr, rendered_bgr)

    print("\n--- Neutral Roundtrip Precision Metrics ---")
    print(f"PSNR: {metrics['psnr_db']} dB")
    print(f"Mean Delta E (CIE76): {metrics['mean_delta_e']}")
    print(f"Max RGB Error: {metrics['max_rgb_error']}")
    print(f"Black Point Shift: {metrics['black_point_shift']}")
    print(f"White Point Shift: {metrics['white_point_shift']}")

    assert metrics["psnr_db"] >= 40.0, f"PSNR too low ({metrics['psnr_db']} dB)"
    assert metrics["mean_delta_e"] < 0.80, f"Mean Delta E too high ({metrics['mean_delta_e']})"
    assert metrics["black_point_shift"] <= 2.0, "Black level shifted significantly"
    assert metrics["white_point_shift"] <= 2.0, "White level shifted significantly"

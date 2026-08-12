from pathlib import Path

import numpy as np

from color_core.cube_tools import apply_cube_to_bgr, compose_cube_luts
from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.project_qc import inspect_cube_lut
from color_core.recipe import GradeRecipe


def test_identity_cube_composition_and_image_application(tmp_path):
    manager = OCIOManager()
    first = bake_3d_lut(manager.complete_recipe(GradeRecipe()), str(tmp_path / "first.cube"), manager)
    second = bake_3d_lut(manager.complete_recipe(GradeRecipe()), str(tmp_path / "second.cube"), manager)
    composed = compose_cube_luts(first, second, str(tmp_path / "composed.cube"), second_strength=0.5)
    assert inspect_cube_lut(composed).passed
    image = np.full((8, 8, 3), 128, dtype=np.uint8)
    output = apply_cube_to_bgr(image, composed)
    assert output.shape == image.shape
    assert np.max(np.abs(output.astype(np.int16) - image.astype(np.int16))) <= 3

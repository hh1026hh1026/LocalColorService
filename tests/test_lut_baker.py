from pathlib import Path

import numpy as np

from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.recipe import GradeRecipe


def _cube_rows(path: Path) -> np.ndarray:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            values = [float(value) for value in line.split()]
        except ValueError:
            continue
        if len(values) == 3:
            rows.append(values)
    return np.asarray(rows)


def test_ocio_baker_33_and_neutral_error(tmp_path):
    manager = OCIOManager()
    output = tmp_path / "neutral.cube"
    bake_3d_lut(GradeRecipe(lut_size=33), str(output), manager)
    text = output.read_text(encoding="utf-8")
    assert "LUT_3D_SIZE 33" in text
    actual = _cube_rows(output)
    values = np.linspace(0.0, 1.0, 33)
    expected = np.asarray([[r, g, b] for b in values for g in values for r in values])
    assert actual.shape == (33**3, 3)
    assert np.max(np.abs(actual - expected)) <= 0.01

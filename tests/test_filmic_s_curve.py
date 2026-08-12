from pathlib import Path

from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.recipe import GradeRecipe


def _cube_rows(path: Path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            values = [float(value) for value in line.split()]
        except ValueError:
            continue
        if len(values) == 3:
            rows.append(values)
    return rows


def test_ocio_log_exposure_is_smooth_and_not_immediately_clipped(tmp_path):
    manager = OCIOManager()
    output = tmp_path / "plus_one.cube"
    bake_3d_lut(GradeRecipe(exposure=1.0), str(output), manager)
    rows = _cube_rows(output)
    mid_gray = rows[16 * (33 * 33 + 33 + 1)]
    assert all(0.55 < value < 0.90 for value in mid_gray)
    assert "using OpenColorIO Baker" in output.read_text(encoding="utf-8")

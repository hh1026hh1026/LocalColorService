"""V0.6 white balance: colorimetric correctness of the linear chromatic adaptation.

The regression these tests lock down: white balance used to be applied as a
per-channel CDL slope while the data sat in ACEScct. A per-channel multiply in a
log encoding is a per-channel gamma, so the neutral axis fanned out - shadows and
highlights drifted in opposite chromaticity directions. The project QC reported
this as "neutral axis colour separation" on precisely the shots carrying a
non-unity rgb_gains.

The defining property of a correct white balance is that it is a single 3x3
matrix in a linear space, so a neutral input ramp must come out with *constant
chromaticity* at every luminance. It does NOT have to come out neutral - a
technical white balance legitimately moves the neutral axis, it just has to move
all of it by the same amount.
"""

from __future__ import annotations

import colour
import numpy as np
import pytest

from color_core.ocio_manager import OCIOManager
from color_core.recipe import GradeRecipe
from color_core.white_balance import (
    creative_white_from_temperature_tint,
    describe_white_balance,
    is_identity_matrix,
    source_white_from_gains,
    white_balance_matrix_ap0,
)

AP0 = colour.RGB_COLOURSPACES["ACES2065-1"]
REC709 = colour.RGB_COLOURSPACES["ITU-R BT.709"]


@pytest.fixture(scope="module")
def manager() -> OCIOManager:
    return OCIOManager()


def _graded_linear(manager: OCIOManager, recipe: GradeRecipe, encoded: np.ndarray) -> np.ndarray:
    """Encoded Rec.709 in, linear ACES2065-1 out, through the real grade group."""
    complete = manager.complete_recipe(recipe)
    config = manager.config
    data = np.ascontiguousarray(encoded, dtype=np.float32).copy()
    config.getProcessor(
        manager.spaces["input"], manager.reference_space
    ).getDefaultCPUProcessor().applyRGB(data.reshape(-1, 3))
    config.getProcessor(
        manager.build_grade_group(complete)
    ).getDefaultCPUProcessor().applyRGB(data.reshape(-1, 3))
    config.getProcessor(
        manager.spaces["output"], manager.reference_space
    ).getDefaultCPUProcessor().applyRGB(data.reshape(-1, 3))
    return data.astype(np.float64)


def _neutral_ramp(steps: int = 19) -> np.ndarray:
    return np.stack([np.linspace(0.05, 0.95, steps)] * 3, axis=-1).astype(np.float32)


def _chromaticities(linear_ap0: np.ndarray) -> np.ndarray:
    return np.array(
        [colour.XYZ_to_xy(AP0.matrix_RGB_to_XYZ @ np.maximum(pixel, 1e-9)) for pixel in linear_ap0]
    )


# --------------------------------------------------------------------------
# Matrix-level properties
# --------------------------------------------------------------------------

def test_default_recipe_yields_exact_identity_matrix():
    assert is_identity_matrix(white_balance_matrix_ap0((1.0, 1.0, 1.0), 0.0, 0.0, 1.0))


def test_zero_strength_disables_the_adaptation():
    matrix = white_balance_matrix_ap0((0.90, 1.0, 1.12), 6.0, 0.2, strength=0.0)
    assert is_identity_matrix(matrix)


def test_adaptation_maps_the_estimated_illuminant_onto_neutral():
    """The whole point of a CAT: the scene illuminant must land on the target white."""
    gains = (0.93, 1.0, 1.12)
    matrix = white_balance_matrix_ap0(gains, 0.0, 0.0, 1.0)
    illuminant_xyz = source_white_from_gains(gains)
    illuminant_ap0 = np.linalg.inv(AP0.matrix_RGB_to_XYZ) @ illuminant_xyz
    adapted = AP0.matrix_RGB_to_XYZ @ (matrix @ illuminant_ap0)
    assert np.allclose(colour.XYZ_to_xy(adapted), REC709.whitepoint, atol=1e-4)


def test_white_balance_is_luminance_preserving():
    """WB must not double as an exposure change."""
    for gains in [(0.93, 1.0, 1.12), (1.10, 1.0, 0.90)]:
        matrix = white_balance_matrix_ap0(gains, 0.0, 0.0, 1.0)
        neutral_ap0 = np.linalg.inv(AP0.matrix_RGB_to_XYZ) @ np.array([0.9505, 1.0, 1.0890])
        before = (AP0.matrix_RGB_to_XYZ @ neutral_ap0)[1]
        after = (AP0.matrix_RGB_to_XYZ @ (matrix @ neutral_ap0))[1]
        assert after == pytest.approx(before, rel=0.02)


def test_temperature_zero_and_tint_zero_is_exactly_d65():
    assert np.allclose(
        colour.XYZ_to_xy(creative_white_from_temperature_tint(0.0, 0.0)),
        REC709.whitepoint,
        atol=1e-9,
    )


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("gains", [[0.93, 1.0, 1.12], [1.08, 1.0, 0.90], [0.88, 1.0, 1.12]])
def test_neutral_axis_chromaticity_is_constant_under_technical_wb(manager, gains):
    """Regression for the ACEScct log-slope white balance.

    With the old implementation this spread measured 0.076 - 0.172, which is on
    the order of the entire D65-to-tungsten chromaticity distance. A correct
    linear matrix leaves it at float noise.
    """
    graded = _graded_linear(manager, GradeRecipe(rgb_gains=gains), _neutral_ramp())
    spread = np.ptp(_chromaticities(graded), axis=0)
    assert spread.max() < 1e-3, f"neutral axis fans out by {spread} for gains={gains}"


def test_neutral_recipe_is_a_pixel_identity(manager):
    ramp = _neutral_ramp()
    complete = manager.complete_recipe(GradeRecipe())
    data = np.ascontiguousarray(ramp, dtype=np.float32).copy()
    config = manager.config
    config.getProcessor(
        manager.spaces["input"], manager.reference_space
    ).getDefaultCPUProcessor().applyRGB(data.reshape(-1, 3))
    config.getProcessor(
        manager.build_grade_group(complete)
    ).getDefaultCPUProcessor().applyRGB(data.reshape(-1, 3))
    config.getProcessor(
        manager.spaces["output"], manager.reference_space
    ).getDefaultCPUProcessor().applyRGB(data.reshape(-1, 3))
    reference = np.ascontiguousarray(ramp, dtype=np.float32).copy()
    config.getProcessor(
        manager.spaces["input"], manager.reference_space
    ).getDefaultCPUProcessor().applyRGB(reference.reshape(-1, 3))
    assert np.abs(data - reference).max() < 1e-4


# --------------------------------------------------------------------------
# Creative temperature / tint semantics
# --------------------------------------------------------------------------

def test_positive_temperature_warms_and_negative_cools(manager):
    mid = np.array([[0.5, 0.5, 0.5]], dtype=np.float32)
    warm = _graded_linear(manager, GradeRecipe(temperature=8.0), mid)[0]
    cool = _graded_linear(manager, GradeRecipe(temperature=-8.0), mid)[0]
    warm_xy = colour.XYZ_to_xy(AP0.matrix_RGB_to_XYZ @ warm)
    cool_xy = colour.XYZ_to_xy(AP0.matrix_RGB_to_XYZ @ cool)
    assert warm_xy[0] > REC709.whitepoint[0] > cool_xy[0]


def test_tint_moves_the_rendered_image_along_green_magenta(manager):
    """Positive tint must render magenta, negative green.

    Note the inversion: tint offsets the *assumed source* white, so a positive
    tint makes the assumed illuminant greener and the adapted picture magenta.
    Asserting on the source white alone would read backwards.
    """
    mid = np.array([[0.5, 0.5, 0.5]], dtype=np.float32)
    magenta = _graded_linear(manager, GradeRecipe(tint=0.25), mid)[0]
    green = _graded_linear(manager, GradeRecipe(tint=-0.25), mid)[0]
    magenta_uv = colour.xy_to_UCS_uv(colour.XYZ_to_xy(AP0.matrix_RGB_to_XYZ @ magenta))
    green_uv = colour.xy_to_UCS_uv(colour.XYZ_to_xy(AP0.matrix_RGB_to_XYZ @ green))
    neutral_uv = colour.xy_to_UCS_uv(REC709.whitepoint)
    assert magenta_uv[1] < neutral_uv[1] < green_uv[1]


def test_recipe_records_white_balance_provenance(manager):
    complete = manager.complete_recipe(GradeRecipe(rgb_gains=[0.93, 1.0, 1.12], temperature=4.0))
    record = complete.white_balance
    assert record["method"] == "cat_bradford_linear"
    assert record["source_cct_kelvin"] > 0
    assert len(record["source_white_xy"]) == 2
    assert record["temperature_mired_offset"] == pytest.approx(8.0)
    assert describe_white_balance([1.0, 1.0, 1.0], 0.0, 0.0)["source_white_xy"] == pytest.approx(
        list(REC709.whitepoint), abs=1e-4
    )

"""V0.6 layered QC: LUT checks must depend on the transform's role.

V0.5.2 already had a ``transform_type`` parameter on the inspector, but every
call site passed ``"combined"``. The result was a wave of warnings that no
colourist could act on: a positive exposure necessarily pushes samples past 1.0,
and a white balance necessarily moves the neutral axis, so every technically
corrected shot tripped both checks.

These tests pin the distinction down: what a technical LUT is allowed to do,
what it is still not allowed to do, and that the two roles reach different
verdicts on the same file.
"""

from __future__ import annotations

import numpy as np
import pytest

from color_core.cube_tools import write_cube
from color_core.project_qc import format_timecode, inspect_cube_lut


def _identity_grid(size: int = 33) -> np.ndarray:
    axis = np.linspace(0.0, 1.0, size)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack((red, green, blue), axis=-1)


def _write(values: np.ndarray, tmp_path, name: str) -> str:
    return write_cube(np.asarray(values, dtype=np.float64), str(tmp_path / name), name)


def _white_balance_lut(tmp_path, gains=(0.90, 1.0, 1.15), name="wb.cube") -> str:
    """A correct white balance: uniform channel gain, applied in linear terms."""
    grid = _identity_grid()
    return _write(np.clip(grid * np.asarray(gains), 0.0, 1.0), tmp_path, name)


def _fanning_axis_lut(tmp_path, name="fan.cube") -> str:
    """Emulates the pre-V0.6 bug: a per-channel gamma, so the axis opens up."""
    grid = _identity_grid()
    powers = np.asarray([1.0, 1.15, 1.32])
    return _write(np.clip(grid ** powers, 0.0, 1.0), tmp_path, name)


# ---------------------------------------------------------------------------
# What a technical LUT is allowed to do
# ---------------------------------------------------------------------------

def test_technical_lut_may_move_the_neutral_axis(tmp_path):
    """A white balance shifts neutrals. That is its job, not a defect."""
    report = inspect_cube_lut(_white_balance_lut(tmp_path), "technical")
    assert report.neutral_axis_max_spread > 0.05, "test fixture should move the axis"
    assert report.state == "PASS"
    assert report.warnings == []


def test_technical_lut_may_exceed_the_display_domain(tmp_path):
    """A positive exposure clips into the top of the cube; not a warning."""
    grid = _identity_grid()
    lifted = np.clip(grid * 1.45, 0.0, 1.0)
    report = inspect_cube_lut(_write(lifted, tmp_path, "exposure.cube"), "technical")
    assert report.in_range_ratio <= 1.0
    assert not any("out-of-range" in warning for warning in report.warnings)
    assert report.state == "PASS"


# ---------------------------------------------------------------------------
# What it is still not allowed to do
# ---------------------------------------------------------------------------

def test_technical_lut_with_a_fanning_neutral_axis_is_flagged(tmp_path):
    """The actual pre-V0.6 defect must still be caught."""
    report = inspect_cube_lut(_fanning_axis_lut(tmp_path), "technical")
    assert report.neutral_axis_chromaticity_drift > 0.010
    assert report.state == "NEEDS_REVIEW"
    assert any("drift" in warning for warning in report.warnings)


def test_uniform_white_balance_has_near_zero_drift(tmp_path):
    report = inspect_cube_lut(_white_balance_lut(tmp_path), "technical")
    assert report.neutral_axis_chromaticity_drift < 1e-6


# ---------------------------------------------------------------------------
# Roles disagree on purpose
# ---------------------------------------------------------------------------

def test_same_lut_reaches_different_verdicts_per_role(tmp_path):
    """This is the whole point of the split."""
    path = _fanning_axis_lut(tmp_path, "shared.cube")
    technical = inspect_cube_lut(path, "technical")
    creative = inspect_cube_lut(path, "creative")
    assert technical.state == "NEEDS_REVIEW"
    # A Look is allowed to tint shadows and highlights differently.
    assert creative.state == "PASS"
    assert technical.transform_type == "technical"
    assert creative.transform_type == "creative"


def test_creative_lut_is_not_checked_for_neutrality(tmp_path):
    grid = _identity_grid()
    tinted = np.clip(grid * np.asarray([1.0, 0.94, 0.86]), 0.0, 1.0)
    report = inspect_cube_lut(_write(tinted, tmp_path, "look.cube"), "creative")
    assert not any("neutral" in warning for warning in report.warnings)


def test_creative_lut_skin_hue_rotation_is_reported(tmp_path):
    """Rotate the skin region hard; the number must surface, not a vague pass."""
    grid = _identity_grid()
    rotated = np.stack(
        (grid[..., 2], grid[..., 0], grid[..., 1]), axis=-1
    )  # channel permutation = extreme hue rotation
    report = inspect_cube_lut(_write(rotated, tmp_path, "rotate.cube"), "creative")
    assert report.skin_hue_rotation_deg > 8.0
    assert report.state in {"NEEDS_REVIEW", "FAIL"}


def test_identity_lut_passes_in_every_role(tmp_path):
    path = _write(_identity_grid(), tmp_path, "identity.cube")
    for role in ("technical", "creative", "combined", "output"):
        report = inspect_cube_lut(path, role)
        assert report.state == "PASS", f"{role} flagged an identity LUT: {report.warnings}"
        assert report.passed


def test_default_role_is_combined(tmp_path):
    assert inspect_cube_lut(_write(_identity_grid(), tmp_path, "d.cube")).transform_type == "combined"


def test_curvature_is_measured_separately_from_gradient(tmp_path):
    """Banding tracks curvature; a steep but smooth ramp should not be flagged."""
    grid = _identity_grid()
    smooth = np.clip(grid * 1.2, 0.0, 1.0)
    report = inspect_cube_lut(_write(smooth, tmp_path, "smooth.cube"), "creative")
    assert report.max_gradient > 0.03
    assert report.max_curvature < 0.06
    assert not any("banding" in warning for warning in report.warnings)


def test_timecode_formatting():
    assert format_timecode(0.0) == "00:00:00.000"
    assert format_timecode(3723.456) == "01:02:03.456"
    assert format_timecode(-5.0) == "00:00:00.000"

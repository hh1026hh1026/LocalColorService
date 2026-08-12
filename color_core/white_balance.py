"""Colorimetric white balance as a linear-domain chromatic adaptation transform.

Why this module exists
----------------------
Until V0.5.2 the technical white balance (``GradeRecipe.rgb_gains``) was applied
as a per-channel CDL *slope* while the data was already encoded in ACEScct. A
per-channel multiply in a logarithmic encoding is not a channel gain: it is a
per-channel *gamma*. The practical consequence is that the neutral axis opens
up - shadows and highlights drift in opposite chromaticity directions - which
is what the project QC reported as "neutral axis colour separation" on exactly
the shots that carried a non-unity ``rgb_gains``.

A white balance is a chromatic adaptation between two illuminants and must be
expressed as a 3x3 matrix in a *linear* colour space. This module derives that
matrix so the caller can insert it into the OCIO GroupTransform while the data
is still linear (the ACES2065-1 reference space).

Layering
--------
Two independent adaptations are composed:

``technical``
    Estimated scene illuminant -> D65. Derived from ``rgb_gains``, which the
    image analyzer produces with a gray-world / shades-of-gray / neutral
    highlight ensemble.

``creative``
    A deliberate white-point offset owned by the Look Packages, expressed as
    ``temperature`` (mired offset) and ``tint`` (Duv offset). Positive
    ``temperature`` warms the picture, positive ``tint`` pushes magenta.

Keeping them separate means the technical layer can be QC'd for neutrality
while the creative layer is allowed to be non-neutral by design.
"""

from __future__ import annotations

import os
from functools import lru_cache

import colour
import numpy as np
from colour.adaptation import matrix_chromatic_adaptation_VonKries

__all__ = [
    "MIRED_PER_TEMPERATURE_UNIT",
    "DUV_PER_TINT_UNIT",
    "gain_linearization_exponent",
    "linear_gains_from_encoded",
    "source_white_from_gains",
    "creative_white_from_temperature_tint",
    "white_balance_matrix",
    "white_balance_matrix_ap0",
    "describe_white_balance",
    "is_identity_matrix",
]


# ``temperature`` is stored in the recipe as an abstract unit in [-50, +50].
# Mired (reciprocal megakelvin) is the perceptually even unit for colour
# temperature and is what physical CTO/CTB gels are graded in: 1/8 CTO is
# roughly 20 mired. Two mired per unit therefore puts the recipe's usable
# range at about +/- 100 mired, i.e. +/- half a CTO - a sane creative span.
MIRED_PER_TEMPERATURE_UNIT = 2.0

# ``tint`` is stored in [-0.5, +0.5]. Duv beyond about +/-0.02 leaves the range
# where correlated colour temperature stays meaningful.
DUV_PER_TINT_UNIT = 0.04

# Range the *measurement* is allowed to express. This is deliberately wide: it
# bounds what illuminant can be described, not what correction gets applied.
#
# The previous [0.70, 1.45] covered roughly 4200-9800K, so anything outside
# saturated. Measured against the golden set, a 3200K tungsten chart reported
# 4392K - a 159 mired error - purely because the gains hit the ceiling
# (3200K needs a blue gain of 2.66). 2500-15000K needs about [0.35, 4.5].
#
# How much correction is actually applied is capped separately and much more
# tightly, in correction_engine and group_white_balance. Measure accurately,
# apply conservatively.
LINEAR_GAIN_MIN = 0.35
LINEAR_GAIN_MAX = 4.50

_ADAPTATION_TRANSFORM = "Bradford"
_REC709 = colour.RGB_COLOURSPACES["ITU-R BT.709"]
_AP0 = colour.RGB_COLOURSPACES["ACES2065-1"]


def _normalized_white(xyz: np.ndarray) -> np.ndarray:
    """Scale an XYZ tristimulus so that Y == 1."""
    xyz = np.asarray(xyz, dtype=np.float64)
    return xyz / max(float(xyz[1]), 1e-12)


_D65_XYZ = _normalized_white(colour.xy_to_XYZ(_REC709.whitepoint))

# D65 does not sit exactly on the Planckian locus (it has a Duv of about
# +0.0032). Anchoring the creative white point on D65's own (CCT, Duv) rather
# than on the blackbody curve guarantees that temperature=0 and tint=0 produce
# an exact identity instead of a small residual cast.
_D65_CCT, _D65_DUV = (
    float(value)
    for value in colour.temperature.uv_to_CCT(
        colour.xy_to_UCS_uv(_REC709.whitepoint), method="Ohno 2013"
    )
)
_D65_MIRED = 1e6 / _D65_CCT


def gain_linearization_exponent() -> float:
    """Exponent converting display-encoded gray-world gains to linear gains.

    ``image_analyzer`` currently estimates white balance on Rec.709
    *encoded* values. A relative channel imbalance ``r`` in the encoded domain
    corresponds to ``r ** gamma`` in linear light, so the encoded gain must be
    raised to the effective decoding gamma before it can be treated as a
    physical channel gain.

    Since V0.6.2 the analyzer estimates white balance in scene-linear light, so
    ``rgb_gains`` are already physical linear gains and the default is 1.0 -
    no conversion. Raise it only when feeding gains produced by an older,
    display-domain estimate.
    """
    try:
        value = float(os.getenv("WB_GAIN_LINEAR_EXPONENT", "1.0"))
    except (TypeError, ValueError):
        return 2.2
    return float(np.clip(value, 1.0, 3.0))


def linear_gains_from_encoded(gains: tuple[float, float, float]) -> np.ndarray:
    exponent = gain_linearization_exponent()
    encoded = np.clip(np.asarray(gains, dtype=np.float64), 1e-3, 1e3)
    return np.clip(encoded ** exponent, LINEAR_GAIN_MIN, LINEAR_GAIN_MAX)


def source_white_from_gains(gains: tuple[float, float, float]) -> np.ndarray:
    """Recover the estimated scene illuminant (XYZ, Y=1) from channel gains.

    The gains are the factors that neutralize the scene, so the illuminant is
    their reciprocal expressed in Rec.709 linear primaries.
    """
    linear = linear_gains_from_encoded(gains)
    return _normalized_white(_REC709.matrix_RGB_to_XYZ @ (1.0 / linear))


def creative_white_from_temperature_tint(temperature: float, tint: float) -> np.ndarray:
    """Target-side white point for the creative temperature/tint offset.

    Implemented as a shift of the *assumed source* white: raising the assumed
    colour temperature makes the adaptation add warmth, which matches how a
    camera's Kelvin control behaves.
    """
    if float(temperature) == 0.0 and float(tint) == 0.0:
        return _D65_XYZ.copy()
    mired = float(
        np.clip(_D65_MIRED - float(temperature) * MIRED_PER_TEMPERATURE_UNIT, 25.0, 1000.0)
    )
    duv = float(np.clip(_D65_DUV + float(tint) * DUV_PER_TINT_UNIT, -0.05, 0.05))
    uv = colour.temperature.CCT_to_uv(np.array([1e6 / mired, duv]), method="Ohno 2013")
    return _normalized_white(colour.xy_to_XYZ(colour.UCS_uv_to_xy(uv)))


def cct_duv_from_white(xyz: np.ndarray) -> tuple[float, float]:
    """Correlated colour temperature and Duv of a white point (XYZ, Y=1)."""
    try:
        cct, duv = colour.temperature.uv_to_CCT(
            colour.xy_to_UCS_uv(colour.XYZ_to_xy(np.asarray(xyz, dtype=np.float64))),
            method="Ohno 2013",
        )
        return float(cct), float(duv)
    except Exception:  # pragma: no cover - far out-of-locus estimates
        return float(_D65_CCT), float(_D65_DUV)


def white_from_cct_duv(cct: float, duv: float) -> np.ndarray:
    """Inverse of :func:`cct_duv_from_white`."""
    safe_cct = float(np.clip(cct, 1000.0, 40000.0))
    safe_duv = float(np.clip(duv, -0.05, 0.05))
    uv = colour.temperature.CCT_to_uv(np.array([safe_cct, safe_duv]), method="Ohno 2013")
    return _normalized_white(colour.xy_to_XYZ(colour.UCS_uv_to_xy(uv)))


# How far the automatic path may move a shot's white point, in mired.
# Roughly matches what the old [0.80, 1.25] gain envelope permitted, but as a
# smooth symmetric limit rather than a hard saturator.
MAX_CORRECTION_MIRED = 40.0


def limit_white_point_shift(
    source_mired: float, target_mired: float, max_shift: float | None = None
) -> float:
    """Bound how far a correction may move the white point, in mired.

    Clamping the *gains* to a fixed envelope looks equivalent but is not: it
    saturates. Measured against the old [0.80, 1.25] envelope, target white
    points of 210, 230 and 260 mired all collapsed to exactly 198.0, and the
    envelope was asymmetric (-29/+43 mired around D65). Shots with quite
    different targets therefore landed on identical values while a shot just
    inside the envelope stayed far from one just outside - the clamp itself
    manufactured discontinuities at cuts.

    Limiting the shift in mired is smooth, monotonic and symmetric, and states
    the intent directly: do not move the white point by more than this much.
    """
    limit = MAX_CORRECTION_MIRED if max_shift is None else float(max_shift)
    shift = float(target_mired) - float(source_mired)
    return float(source_mired) + float(np.clip(shift, -limit, limit))


def gains_from_source_white(xyz: np.ndarray) -> list[float]:
    """Inverse of :func:`source_white_from_gains`: white point back to recipe gains.

    Used by the scene-group white-balance harmoniser, which reasons in mired and
    Duv (perceptually even) and has to write the result back as ``rgb_gains``.
    """
    linear_rgb = np.linalg.inv(_REC709.matrix_RGB_to_XYZ) @ _normalized_white(xyz)
    linear_gains = 1.0 / np.clip(linear_rgb, 1e-6, None)
    linear_gains = linear_gains / max(linear_gains[1], 1e-9)
    exponent = gain_linearization_exponent()
    encoded = np.clip(linear_gains, LINEAR_GAIN_MIN, LINEAR_GAIN_MAX) ** (1.0 / exponent)
    return [float(value) for value in encoded / max(encoded[1], 1e-9)]


@lru_cache(maxsize=512)
def _white_balance_matrix_cached(
    gains: tuple[float, float, float],
    temperature: float,
    tint: float,
    strength: float,
    basis_name: str,
    exponent: float,  # part of the cache key so env changes invalidate it
) -> tuple[float, ...]:
    basis = colour.RGB_COLOURSPACES[basis_name]
    technical = matrix_chromatic_adaptation_VonKries(
        source_white_from_gains(gains), _D65_XYZ, transform=_ADAPTATION_TRANSFORM
    )
    creative = matrix_chromatic_adaptation_VonKries(
        creative_white_from_temperature_tint(temperature, tint),
        _D65_XYZ,
        transform=_ADAPTATION_TRANSFORM,
    )
    xyz_matrix = creative @ technical
    rgb_matrix = (
        np.linalg.inv(basis.matrix_RGB_to_XYZ) @ xyz_matrix @ basis.matrix_RGB_to_XYZ
    )
    amount = float(np.clip(strength, 0.0, 1.0))
    blended = np.eye(3) + (rgb_matrix - np.eye(3)) * amount
    return tuple(blended.reshape(-1).tolist())


def white_balance_matrix(
    gains: tuple[float, float, float] | list[float] = (1.0, 1.0, 1.0),
    temperature: float = 0.0,
    tint: float = 0.0,
    strength: float = 1.0,
    basis_name: str = "ACES2065-1",
) -> np.ndarray:
    """Return the 3x3 white balance matrix in the requested linear RGB basis."""
    key = tuple(round(float(value), 9) for value in gains)
    flat = _white_balance_matrix_cached(
        key,
        round(float(temperature), 9),
        round(float(tint), 9),
        round(float(strength), 9),
        basis_name,
        gain_linearization_exponent(),
    )
    return np.asarray(flat, dtype=np.float64).reshape(3, 3)


def white_balance_matrix_ap0(
    gains: tuple[float, float, float] | list[float] = (1.0, 1.0, 1.0),
    temperature: float = 0.0,
    tint: float = 0.0,
    strength: float = 1.0,
) -> np.ndarray:
    """White balance matrix in ACES2065-1 (AP0), the OCIO scene reference space."""
    return white_balance_matrix(gains, temperature, tint, strength, "ACES2065-1")


def is_identity_matrix(matrix: np.ndarray, tolerance: float = 1e-9) -> bool:
    return bool(np.allclose(np.asarray(matrix, dtype=np.float64), np.eye(3), atol=tolerance))


def describe_white_balance(
    gains: tuple[float, float, float] | list[float] = (1.0, 1.0, 1.0),
    temperature: float = 0.0,
    tint: float = 0.0,
) -> dict:
    """Human- and audit-readable summary of the adaptation being performed."""
    source = source_white_from_gains(gains)
    creative = creative_white_from_temperature_tint(temperature, tint)
    source_xy = colour.XYZ_to_xy(source)
    creative_xy = colour.XYZ_to_xy(creative)
    try:
        source_cct, source_duv = (
            float(value)
            for value in colour.temperature.uv_to_CCT(
                colour.xy_to_UCS_uv(source_xy), method="Ohno 2013"
            )
        )
    except Exception:  # pragma: no cover - far out-of-locus estimates
        source_cct, source_duv = 0.0, 0.0
    return {
        "method": "cat_bradford_linear",
        "adaptation_transform": _ADAPTATION_TRANSFORM,
        "gain_linearization_exponent": round(gain_linearization_exponent(), 4),
        "linear_gains": [round(float(value), 6) for value in linear_gains_from_encoded(gains)],
        "source_white_xy": [round(float(value), 6) for value in source_xy],
        "source_cct_kelvin": round(source_cct, 1),
        "source_duv": round(source_duv, 6),
        "creative_white_xy": [round(float(value), 6) for value in creative_xy],
        "target_white_xy": [round(float(value), 6) for value in _REC709.whitepoint],
        "temperature_mired_offset": round(float(temperature) * MIRED_PER_TEMPERATURE_UNIT, 3),
        "tint_duv_offset": round(float(tint) * DUV_PER_TINT_UNIT, 6),
    }

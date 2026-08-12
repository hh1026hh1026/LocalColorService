"""Synthesise the objective colour-accuracy test set.

Why this exists
---------------
Through seven rounds of changes the only evidence that any of them helped was
unit tests plus subjective viewing. That is not enough to answer "how accurate
is it", and it let a real methodology error through: in V0.6 and V0.6.2 the
algorithms *and* the metrics measuring them were changed in the same release, so
before/after numbers were taken with different rulers and could not be compared.

A synthetic golden set fixes that. Every patch here has a known ground-truth
value, so accuracy is a measurement rather than an opinion, and the numbers stay
comparable across releases.

Everything is generated, not photographed - no licensing, fully reproducible,
and the ground truth is exact.

Outputs (16-bit PNG, Rec.709 primaries, D65):
    colorchecker_d65.png       24-patch chart under the reference illuminant
    colorchecker_<cct>k.png    the same chart under tungsten/cool illuminants
    gray_ramp.png              21-step neutral ramp, for neutral-axis checks
    skin_panel_<cct>k.png      six skin tones under several illuminants
    golden_set.json            ground truth for every patch

Usage:
    python scripts/build_golden_set.py [--output test_assets/golden]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import colour
import numpy as np

REC709 = colour.RGB_COLOURSPACES["ITU-R BT.709"]

# ColorChecker Classic, sRGB/Rec.709 reference values (0-1), in chart order.
COLORCHECKER = {
    "dark skin": (0.4500, 0.3196, 0.2627), "light skin": (0.7627, 0.5804, 0.5020),
    "blue sky": (0.3647, 0.4784, 0.6157),  "foliage": (0.3451, 0.4235, 0.2667),
    "blue flower": (0.5059, 0.5020, 0.6902), "bluish green": (0.3922, 0.7412, 0.6667),
    "orange": (0.8431, 0.4784, 0.1569),    "purplish blue": (0.2745, 0.3569, 0.6431),
    "moderate red": (0.7686, 0.3255, 0.3804), "purple": (0.3608, 0.2314, 0.4235),
    "yellow green": (0.6235, 0.7333, 0.2510), "orange yellow": (0.8902, 0.6314, 0.1765),
    "blue": (0.2000, 0.2431, 0.5804),      "green": (0.2745, 0.5765, 0.2863),
    "red": (0.6863, 0.1961, 0.2275),       "yellow": (0.9294, 0.7804, 0.1255),
    "magenta": (0.7333, 0.3294, 0.5765),   "cyan": (0.0000, 0.5216, 0.6588),
    "white 9.5": (0.9569, 0.9569, 0.9490), "neutral 8": (0.7882, 0.7882, 0.7843),
    "neutral 6.5": (0.6314, 0.6353, 0.6314), "neutral 5": (0.4745, 0.4745, 0.4745),
    "neutral 3.5": (0.3294, 0.3294, 0.3294), "black 2": (0.1961, 0.1961, 0.1961),
}

# Six skin tones spanning light to deep, Rec.709 encoded.
SKIN_TONES = {
    "skin_1_very_light": (0.878, 0.729, 0.639),
    "skin_2_light": (0.800, 0.616, 0.514),
    "skin_3_medium": (0.702, 0.510, 0.404),
    "skin_4_medium_deep": (0.573, 0.392, 0.302),
    "skin_5_deep": (0.435, 0.286, 0.220),
    "skin_6_very_deep": (0.318, 0.204, 0.157),
}

# Illuminants the charts are rendered under. D65 is the reference; the others
# are what an uncorrected white balance has to recover from.
ILLUMINANTS = {"d65": 6503.5, "tungsten_3200k": 3200.0, "warm_4300k": 4300.0, "cool_9000k": 9000.0}


def encode_rec709(linear: np.ndarray) -> np.ndarray:
    value = np.clip(np.asarray(linear, dtype=np.float64), 0.0, 1.0)
    return np.where(value < 0.018, value * 4.5, 1.099 * value ** 0.45 - 0.099)


def decode_rec709(encoded: np.ndarray) -> np.ndarray:
    value = np.clip(np.asarray(encoded, dtype=np.float64), 0.0, 1.0)
    return np.where(value <= 0.081, value / 4.5, ((value + 0.099) / 1.099) ** (1.0 / 0.45))


def illuminant_gains(cct: float) -> np.ndarray:
    """Linear Rec.709 gains simulating capture under a given illuminant.

    A patch lit by tungsten and captured without correction reads warm; these
    gains produce that, so the analyzer has something real to recover.
    """
    if abs(cct - 6503.5) < 1.0:
        return np.ones(3)
    uv = colour.temperature.CCT_to_uv(np.array([cct, 0.0]), method="Ohno 2013")
    xyz = colour.xy_to_XYZ(colour.UCS_uv_to_xy(uv))
    rgb = np.linalg.inv(REC709.matrix_RGB_to_XYZ) @ (xyz / xyz[1])
    gains = rgb / rgb[1]
    return gains / gains.max() * 1.0


def render_patches(
    patches: dict[str, tuple[float, float, float]], cct: float, patch_px: int = 96, columns: int = 6
) -> tuple[np.ndarray, dict]:
    gains = illuminant_gains(cct)
    names = list(patches)
    rows = (len(names) + columns - 1) // columns
    image = np.zeros((rows * patch_px, columns * patch_px, 3), dtype=np.uint16)
    truth: dict[str, dict] = {}
    for index, name in enumerate(names):
        reference = np.asarray(patches[name], dtype=np.float64)
        observed = encode_rec709(decode_rec709(reference) * gains)
        row, column = divmod(index, columns)
        block = np.round(np.clip(observed, 0, 1) * 65535).astype(np.uint16)
        image[row * patch_px:(row + 1) * patch_px, column * patch_px:(column + 1) * patch_px] = block[::-1]
        truth[name] = {
            "reference_rec709": [round(float(v), 6) for v in reference],
            "observed_rec709": [round(float(v), 6) for v in observed],
            "patch_row": row, "patch_column": column,
        }
    return image, truth


def neutral_average_scene(seed: int = 20260807, count: int = 144) -> dict[str, tuple[float, float, float]]:
    """Surface reflectances whose *linear average is neutral* by construction.

    This is the only fair test of an illuminant estimator that assumes
    gray-world. A ColorChecker is not: 24 deliberately saturated patches do not
    average to grey, so measuring gray-world against one scores the scene's
    violation of the assumption rather than the estimator's accuracy.

    The set is built by drawing chromatic reflectances and then adding their
    exact complement, so the mean is neutral to floating-point precision while
    the individual samples stay varied and realistic.
    """
    rng = np.random.default_rng(seed)
    base_linear = 0.18  # mid-grey reflectance
    half = count // 2
    # Chromatic offsets in linear RGB, bounded so nothing leaves the cube.
    offsets = rng.uniform(-0.11, 0.11, (half, 3))
    linear = np.concatenate([base_linear + offsets, base_linear - offsets])
    linear = np.clip(linear, 0.01, 0.95)
    # Restore exact neutrality after the clip.
    linear += base_linear - linear.mean(axis=0)
    linear = np.clip(linear, 0.01, 0.95)
    patches = {}
    for index, value in enumerate(linear):
        patches[f"surface_{index:03d}"] = tuple(float(v) for v in encode_rec709(value))
    return patches


def gray_ramp(steps: int = 21, patch_px: int = 96) -> tuple[np.ndarray, dict]:
    values = np.linspace(0.0, 1.0, steps)
    image = np.zeros((patch_px, steps * patch_px, 3), dtype=np.uint16)
    truth = {}
    for index, value in enumerate(values):
        block = int(round(value * 65535))
        image[:, index * patch_px:(index + 1) * patch_px] = block
        truth[f"gray_{index:02d}"] = {
            "reference_rec709": [round(float(value), 6)] * 3, "patch_column": index,
        }
    return image, truth


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the colour-accuracy golden set")
    parser.add_argument("--output", default="test_assets/golden")
    parser.add_argument("--patch-px", type=int, default=96)
    args = parser.parse_args()

    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "description": "Synthetic colour-accuracy reference. Ground truth is exact by construction.",
        "encoding": "Rec.709 OETF, D65, 16-bit PNG",
        "illuminants": ILLUMINANTS,
        "charts": {},
    }

    for label, cct in ILLUMINANTS.items():
        image, truth = render_patches(COLORCHECKER, cct, args.patch_px)
        name = f"colorchecker_{label}.png"
        cv2.imwrite(str(root / name), image)
        manifest["charts"][name] = {"kind": "colorchecker", "cct": cct, "patches": truth}

        image, truth = render_patches(SKIN_TONES, cct, args.patch_px, columns=3)
        name = f"skin_panel_{label}.png"
        cv2.imwrite(str(root / name), image)
        manifest["charts"][name] = {"kind": "skin", "cct": cct, "patches": truth}

        # The fair illuminant-estimation test: gray-world's assumption holds.
        image, truth = render_patches(
            neutral_average_scene(), cct, max(16, args.patch_px // 4), columns=12
        )
        name = f"neutral_scene_{label}.png"
        cv2.imwrite(str(root / name), image)
        manifest["charts"][name] = {"kind": "neutral_average_scene", "cct": cct, "patches": truth}

    image, truth = gray_ramp(patch_px=args.patch_px)
    cv2.imwrite(str(root / "gray_ramp.png"), image)
    manifest["charts"]["gray_ramp.png"] = {"kind": "gray_ramp", "cct": 6503.5, "patches": truth}

    (root / "golden_set.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Golden set written to {root}")
    for name in sorted(manifest["charts"]):
        print(f"  {name}  ({len(manifest['charts'][name]['patches'])} patches)")


if __name__ == "__main__":
    main()

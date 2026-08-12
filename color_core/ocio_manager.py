"""Strict ACES Studio configuration and OCIO GroupTransform construction."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import PyOpenColorIO as ocio

from color_core.recipe import GradeRecipe
from color_core.white_balance import (
    describe_white_balance,
    is_identity_matrix,
    white_balance_matrix_ap0,
)


def _to_ocio_matrix44(matrix: np.ndarray) -> list[float]:
    """Expand a 3x3 linear matrix into OCIO's row-major 4x4 layout."""
    expanded = np.eye(4, dtype=np.float64)
    expanded[:3, :3] = np.asarray(matrix, dtype=np.float64)
    return [float(value) for value in expanded.reshape(-1)]


class OCIOManager:
    def __init__(self, config_path: str | None = None):
        self.config, self.config_id = self._load_config(config_path)
        self.available_color_spaces = [cs.getName() for cs in self.config.getColorSpaces()]
        self.spaces = self._resolve_required_spaces()
        self.reference_space = self.config.getRoleColorSpace("aces_interchange")
        if not self.reference_space or self.reference_space not in self.available_color_spaces:
            raise RuntimeError("ACES Studio Config has no valid aces_interchange role")

    @staticmethod
    def _load_config(config_path: str | None) -> tuple[Any, str]:
        path = config_path or os.getenv("OCIO_CONFIG_PATH", "")
        if path and Path(path).is_file():
            config = ocio.Config.CreateFromFile(path)
            config.validate()
            return config, str(Path(path).resolve())
        names = [entry[0] for entry in ocio.BuiltinConfigRegistry().getBuiltinConfigs() if entry[0].startswith("studio-config-")]
        failures: list[str] = []
        for name in reversed(names):
            try:
                config = ocio.Config.CreateFromBuiltinConfig(name)
                config.validate()
                return config, name
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        raise RuntimeError("Unable to load an ACES Studio Config: " + "; ".join(failures))

    def _resolve_required_spaces(self) -> dict[str, str]:
        names = self.available_color_spaces
        working = next((name for name in names if name.casefold() == "acescct"), "")
        rec709 = [name for name in names if "rec.709" in name.casefold() or "rec709" in name.casefold()]
        encoded = [name for name in rec709 if "2.4" in name.casefold() and ("encoded" in name.casefold() or "texture" in name.casefold())]
        io_space = encoded[0] if encoded else next((name for name in rec709 if "camera" in name.casefold()), "")
        if not working or not io_space:
            raise RuntimeError("Loaded OCIO config lacks usable ACEScct and SDR Rec.709 color spaces")
        self.config.getProcessor(io_space, working)
        self.config.getProcessor(working, io_space)
        return {"input": io_space, "working": working, "output": io_space}

    def list_color_spaces(self) -> list[str]:
        return list(self.available_color_spaces)

    def resolve_color_space(self, category: str, requested_name: str = "") -> str:
        if category not in self.spaces:
            raise ValueError(f"Unknown color-space category: {category}")
        if not requested_name:
            return self.spaces[category]
        if requested_name in self.available_color_spaces:
            return requested_name
        lowered = requested_name.casefold()
        if category == "working" and "acescct" in lowered:
            return self.spaces[category]
        if category in ("input", "output") and ("709" in lowered or "srgb" in lowered):
            return self.spaces[category]
        raise ValueError(f"OCIO color space is not present in loaded config: {requested_name}")

    @staticmethod
    def _recipe_digest(data: dict[str, Any]) -> str:
        canonical = dict(data)
        canonical.pop("created_at", None)
        canonical.pop("recipe_hash", None)
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def complete_recipe(self, recipe: GradeRecipe) -> GradeRecipe:
        data = recipe.model_dump(mode="json")
        data.update(
            recipe_version="0.6.0",
            service_version="0.6.0",
            white_balance=describe_white_balance(
                recipe.rgb_gains, recipe.temperature, recipe.tint
            ),
            ocio_config_id=self.config_id,
            ocio_config_name=self.config_id,
            input_color_space=self.resolve_color_space("input", recipe.input_color_space),
            working_color_space=self.resolve_color_space("working", recipe.working_color_space),
            output_color_space=self.resolve_color_space("output", recipe.output_color_space),
        )
        data["recipe_hash"] = self._recipe_digest(data)
        return GradeRecipe.model_validate(data)

    def build_grade_group(self, recipe: GradeRecipe) -> Any:
        recipe = self.complete_recipe(recipe)
        strength = recipe.strength
        group = ocio.GroupTransform()

        # ACES Reference Gamut Compression is applied immediately after the input transform,
        # before creative operations, as recommended by the Academy. The scalar remains useful
        # metadata for later semantic protection; in V0.2 any positive value enables the RGC.
        look_names = {look.getName() for look in self.config.getLooks()}
        if recipe.gamut_protection > 0.0 and "ACES 1.3 Reference Gamut Compression" in look_names:
            group.appendTransform(ocio.LookTransform(
                src=self.reference_space,
                dst=self.reference_space,
                looks="ACES 1.3 Reference Gamut Compression",
            ))
        # White balance is a chromatic adaptation between two illuminants and is
        # only meaningful as a 3x3 matrix in a LINEAR space. It therefore runs
        # here, in the scene reference space (ACES2065-1), and no longer as a
        # per-channel CDL slope inside ACEScct. A per-channel multiply in a log
        # encoding behaves as a per-channel gamma, which opens the neutral axis
        # up: shadows and highlights drift in opposite chromaticity directions.
        # See color_core/white_balance.py for the derivation.
        wb_matrix = white_balance_matrix_ap0(
            tuple(recipe.rgb_gains), recipe.temperature, recipe.tint, strength
        )
        if not is_identity_matrix(wb_matrix):
            group.appendTransform(ocio.MatrixTransform(_to_ocio_matrix44(wb_matrix)))

        group.appendTransform(ocio.ColorSpaceTransform(src=self.reference_space, dst=recipe.working_color_space))

        exposure_contrast = ocio.ExposureContrastTransform()
        exposure_contrast.setStyle(ocio.EXPOSURE_CONTRAST_LOGARITHMIC)
        exposure_contrast.setExposure(recipe.exposure * strength)
        exposure_contrast.setContrast(1.0 + (recipe.contrast - 1.0) * strength)
        exposure_contrast.setPivot(recipe.pivot)
        group.appendTransform(exposure_contrast)

        # rgb_gains / temperature / tint have already been applied as a linear
        # chromatic adaptation above. What remains here is the creative CDL
        # slope, which is genuinely a log-domain printer-light style operation.
        slope = [1.0 + (recipe.gain[i] - 1.0) * strength for i in range(3)]
        # A small additive shadow bias makes the Look Package shadow-color field effective.
        # Values are intentionally restrained because CDL offset also influences near-black code values.
        offset = [(recipe.lift[i] + recipe.shadow_color[i]) * strength for i in range(3)]
        power = [1.0 / max(0.01, 1.0 + (recipe.gamma[i] - 1.0) * strength) for i in range(3)]
        cdl = ocio.CDLTransform()
        cdl.setStyle(ocio.CDL_NO_CLAMP)
        cdl.setSlope(slope)
        cdl.setOffset(offset)
        cdl.setPower(power)
        cdl.setSat(1.0 + (recipe.saturation - 1.0) * strength)
        group.appendTransform(cdl)

        # Creative tone is kept separate from the technical CDL layer.
        # ``highlight_rolloff`` is the broad HDR shoulder control while
        # ``highlight_softness`` is the gentler highlight transition.  Both
        # use the same ACES grading-tone primitive so they remain visible in
        # baked LUTs and in the project QC pass instead of being metadata-only.
        if recipe.highlight_softness > 0.0 or recipe.highlight_rolloff > 0.0 or recipe.color_density != 0.0:
            tone_values = ocio.GradingTone(ocio.GRADING_LOG)
            softness = min(1.0, max(0.0, recipe.highlight_softness))
            rolloff = min(1.0, max(0.0, recipe.highlight_rolloff))
            tone_values.highlights.master = 1.0 - 0.18 * max(softness, rolloff) * strength
            tone_values.whites.master = 1.0 - 0.10 * (0.45 * softness + 0.55 * rolloff) * strength
            tone_values.midtones.master = 1.0 + 0.06 * recipe.color_density * strength
            tone = ocio.GradingToneTransform(ocio.GRADING_LOG)
            tone.setValue(tone_values)
            group.appendTransform(tone)

        group.appendTransform(ocio.ColorSpaceTransform(src=recipe.working_color_space, dst=recipe.output_color_space))
        group.validate()
        return group

    def transform_rgb_array(self, rgb_array: np.ndarray, src_space: str, dst_space: str) -> np.ndarray:
        src_category = "working" if "acescct" in src_space.casefold() else "input"
        dst_category = "working" if "acescct" in dst_space.casefold() else "output"
        src = self.resolve_color_space(src_category, src_space)
        dst = self.resolve_color_space(dst_category, dst_space)
        processor = self.config.getProcessor(src, dst).getDefaultCPUProcessor()
        result = np.ascontiguousarray(rgb_array, dtype=np.float32).copy()
        processor.applyRGB(result.reshape(-1, 3))
        return result

"""Reference-image A/B/C candidate generation without per-frame stylization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2

from color_core.look_packages import apply_look_package
from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.reinhard_transfer import generate_recipe_from_reference


def generate_reference_candidates(
    source_frame,
    reference_frame,
    output_dir: str,
    manager: OCIOManager,
) -> list[dict[str, Any]]:
    """Build restrained, balanced, and cinematic variants from one reference match."""
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    base = generate_recipe_from_reference(source_frame, reference_frame)
    specifications = [
        ("A", "restrained", "neutral_broadcast", 0.45),
        ("B", "balanced", "clean_commercial", 0.65),
        ("C", "cinematic", "cinematic_soft_print", 0.70),
    ]
    candidates: list[dict[str, Any]] = []
    for candidate_id, label, look_id, strength in specifications:
        recipe = apply_look_package(base.model_copy(deep=True), look_id, strength)
        recipe = manager.complete_recipe(recipe)
        lut_path = bake_3d_lut(recipe, str(directory / f"candidate_{candidate_id.lower()}.cube"), manager)
        candidates.append({
            "id": candidate_id, "label": label, "look_id": look_id,
            "strength": strength, "recipe": recipe.model_dump(mode="json"), "lut_path": lut_path,
        })
    return candidates


def load_reference_frame(path: str):
    image = cv2.imread(str(Path(path).resolve()))
    if image is None:
        raise ValueError(f"Reference image is not readable: {path}")
    return image

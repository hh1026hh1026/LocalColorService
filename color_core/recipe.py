"""Validated and reproducible grade recipe for the V0.2 pipeline."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field, field_validator


class GradeRecipe(BaseModel):
    # 0.6.0 moves the technical white balance out of the ACEScct CDL slope and
    # into a linear-domain chromatic adaptation matrix. Recipes baked before
    # this version produce different pixels and must be re-approved.
    recipe_version: str = "0.6.0"
    service_version: str = "0.6.0"
    ocio_config_id: str = ""
    ocio_config_name: str = ""
    input_color_space: str = ""
    working_color_space: str = ""
    output_color_space: str = ""
    output_standard: str = "SDR Rec.709"

    exposure: float = Field(default=0.0, ge=-2.0, le=2.0)
    temperature: float = Field(default=0.0, ge=-50.0, le=50.0)
    tint: float = Field(default=0.0, ge=-0.5, le=0.5)
    rgb_gains: list[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    lift: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    gamma: list[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    gain: list[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    contrast: float = Field(default=1.0, ge=0.5, le=1.5)
    pivot: float = Field(default=0.18, ge=0.01, le=1.0)
    saturation: float = Field(default=1.0, ge=0.0, le=2.0)
    highlight_rolloff: float = Field(default=0.0, ge=0.0, le=1.0)
    highlight_softness: float = Field(default=0.0, ge=0.0, le=1.0)
    color_density: float = Field(default=0.0, ge=-1.0, le=1.0)
    shadow_color: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    skin_protection: float = Field(default=0.0, ge=0.0, le=1.0)
    gamut_protection: float = Field(default=0.0, ge=0.0, le=1.0)
    look_id: str = "neutral_broadcast"
    look_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    filmic_s_curve: bool = True
    filmic_toe_shoulder: list[float] = Field(default_factory=lambda: [0.15, 0.85])
    lut_size: int = 33
    strength: float = Field(default=1.0, ge=0.0, le=1.0)

    recommended: bool = True
    applied: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationales: list[str] = Field(default_factory=list)
    safety_bounds: dict[str, str] = Field(default_factory=dict)
    applied_safety_bounds: dict[str, str] = Field(default_factory=dict)
    # Auditable provenance for every exposed control.  The renderer does not
    # depend on these fields; they explain whether a value came from measured
    # analysis, a Look package, a quality policy, or a safe default.
    parameter_sources: dict[str, str] = Field(default_factory=dict)
    parameter_confidence: dict[str, float] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    source_media_hash: str = ""
    recipe_hash: str = ""

    # Populated by OCIOManager.complete_recipe. Records how rgb_gains /
    # temperature / tint were realised colorimetrically so QC and provenance do
    # not have to re-derive it. See color_core/white_balance.py.
    white_balance: dict[str, Any] = Field(default_factory=dict)

    # How the exposure figure was arrived at: which anchor, what the measurement
    # asked for before clamping, and whether highlight headroom cut it short.
    # Persisted because "the automatic pass could not do what the shot needed"
    # is the single most useful thing to put in front of a human reviewer, and
    # it was previously only available on the in-memory advice object.
    exposure_diagnostics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rgb_gains", "lift", "gamma", "gain", "shadow_color", "filmic_toe_shoulder")
    @classmethod
    def validate_vectors(cls, value: list[float], info) -> list[float]:
        required = 2 if info.field_name == "filmic_toe_shoulder" else 3
        if len(value) != required:
            raise ValueError(f"{info.field_name} must contain exactly {required} values")
        return [float(v) for v in value]

    @field_validator("lut_size")
    @classmethod
    def validate_lut_size(cls, value: int) -> int:
        if value not in (33, 65):
            raise ValueError("lut_size must be 33 or 65")
        return value

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "GradeRecipe":
        return cls.model_validate(data)

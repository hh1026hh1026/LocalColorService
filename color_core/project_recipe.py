"""Versioned GradePlan schema shared by automatic and assisted workflows.

V0.3 keeps the V0.2 shot fields readable while adding real scene groups,
optimistic revisions, approvals, and provider-owned creative transforms.
Formal rendering must use an approved plan; previews may use drafts.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from color_core.look_packages import apply_look_package
from color_core.recipe import GradeRecipe
from color_core.scene_analysis import SceneAnalysisResult


class ShotGrade(BaseModel):
    scene_id: str
    shot_id: str = ""
    start_time: float
    end_time: float
    base_correction: GradeRecipe
    shot_match: GradeRecipe | None = None
    secondary_grade: dict = Field(default_factory=dict)
    look_strength: float = Field(default=1.0, ge=0.0, le=1.0)
    enabled: bool = True
    base_grade_policy: Literal["auto", "preserve", "manual_review"] = "auto"
    creative_policy: Literal["inherit", "restrained", "bypass", "manual_review"] = "inherit"

    @model_validator(mode="after")
    def populate_shot_id(self):
        if not self.shot_id:
            self.shot_id = self.scene_id
        return self


class TransformAsset(BaseModel):
    """Auditable creative transform produced by a provider."""

    provider: str
    transform_type: Literal["technical", "creative", "combined", "output"] = "creative"
    artifact_id: str = ""
    sha256: str = ""
    relative_path: str = ""
    asset_path: str = ""
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    provider_version: str = ""
    source_candidate_job_id: str = ""
    fit_error: float | None = Field(default=None, ge=0.0)
    fallback_used: bool = False
    metadata: dict = Field(default_factory=dict)


class SceneGroup(BaseModel):
    """A user-editable group of shots that shares one creative transform."""

    scene_group_id: str
    shot_ids: list[str]
    hero_shot_id: str
    label: str = ""
    creative_transform: TransformAsset | None = None
    approved_candidate_id: str | None = None
    creative_source: Literal[
        "project_look_inherited", "provider_candidate", "provider_fallback", "bypass"
    ] = "project_look_inherited"
    approval_state: Literal["pending", "approved_by_project", "approved_candidate"] = "pending"
    fallback_used: bool = False
    # V0.6 SceneGroup V2: measured colour spread inside the group, so the
    # grouping stage's own coherence claim is auditable rather than implicit.
    diagnostics: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_membership(self):
        if not self.shot_ids:
            raise ValueError("scene group must contain at least one shot")
        if len(set(self.shot_ids)) != len(self.shot_ids):
            raise ValueError("scene group contains duplicate shot ids")
        if self.hero_shot_id not in self.shot_ids:
            raise ValueError("hero shot must belong to the scene group")
        return self


class GradePlanEvent(BaseModel):
    revision: int = Field(ge=1)
    operation: str
    actor: str = "local-user"
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    details: dict = Field(default_factory=dict)


class ProjectGradeRecipe(BaseModel):
    recipe_version: str = "0.4.0"
    revision: int = Field(default=1, ge=1)
    workflow: Literal["automatic", "professional_assisted", "reference_assisted"] = "professional_assisted"
    quality_profile: Literal["broadcast_safe", "balanced", "creative"] = "balanced"
    status: Literal["draft", "awaiting_approval", "approved"] = "draft"
    source_path: str
    scene_analysis_job_id: str = ""
    source_media_hash: str = ""
    source_artifact_id: str = ""
    input_transform: dict = Field(default_factory=lambda: {"standard": "SDR Rec.709"})
    project_look: str = "neutral_broadcast"
    shots: list[ShotGrade]
    scene_groups: list[SceneGroup] = Field(default_factory=list)
    output_transform: dict = Field(default_factory=lambda: {"standard": "SDR Rec.709"})
    qc_constraints: dict = Field(default_factory=lambda: {
        "max_adjacent_luminance_jump": 0.25,
        "max_adjacent_temperature_jump": 12.0,
        "max_highlight_clipping_ratio": 0.06,
    })
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    approved_at: str | None = None
    approved_by: str | None = None
    approval_bundle_path: str = ""
    approval_manifest: dict = Field(default_factory=dict)
    events: list[GradePlanEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_timeline(self):
        previous_end = -1.0
        for shot in self.shots:
            if shot.start_time < previous_end - 0.002 or shot.end_time <= shot.start_time:
                raise ValueError("shots must be ordered, non-overlapping, and have positive duration")
            previous_end = shot.end_time
        shot_ids = [shot.shot_id or shot.scene_id for shot in self.shots]
        if not self.scene_groups and shot_ids:
            self.scene_groups = [
                SceneGroup(scene_group_id=f"group_{index + 1:04d}", shot_ids=[shot_id], hero_shot_id=shot_id)
                for index, shot_id in enumerate(shot_ids)
            ]
        grouped = [shot_id for group in self.scene_groups for shot_id in group.shot_ids]
        if sorted(grouped) != sorted(shot_ids):
            raise ValueError("scene groups must contain every shot exactly once")
        return self

    def group_for_shot(self, shot_id: str) -> SceneGroup | None:
        return next((group for group in self.scene_groups if shot_id in group.shot_ids), None)

    def revise(self, operation: str, actor: str = "local-user", details: dict | None = None) -> None:
        self.revision += 1
        self.status = "draft"
        self.approved_at = None
        self.approved_by = None
        self.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.events.append(GradePlanEvent(
            revision=self.revision, operation=operation, actor=actor, details=details or {},
        ))

    def approve(self, actor: str = "local-user") -> None:
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        self.revision += 1
        self.status = "approved"
        self.approved_at = timestamp
        self.approved_by = actor
        self.updated_at = timestamp
        self.events.append(GradePlanEvent(revision=self.revision, operation="approve", actor=actor))

    def resolve_transform_path(self, transform: TransformAsset) -> str:
        """Resolve a frozen relative asset while retaining legacy absolute-path support."""
        if transform.relative_path and self.approval_bundle_path:
            from pathlib import Path

            candidate = Path(self.approval_bundle_path) / transform.relative_path
            if candidate.is_file():
                return str(candidate.resolve())
        return transform.asset_path

    def effective_recipe(self, shot: ShotGrade) -> GradeRecipe:
        base = self.technical_recipe(shot)
        group = self.group_for_shot(shot.shot_id or shot.scene_id)
        group_strength = group.creative_transform.strength if group and group.creative_transform else 1.0
        profile = {
            "broadcast_safe": {"look_scale": 0.75, "skin_min": 0.95, "gamut_min": 0.98, "rolloff_min": 0.24, "softness_min": 0.20},
            "balanced": {"look_scale": 1.00, "skin_min": 0.85, "gamut_min": 0.90, "rolloff_min": 0.10, "softness_min": 0.10},
            "creative": {"look_scale": 1.00, "skin_min": 0.72, "gamut_min": 0.82, "rolloff_min": 0.04, "softness_min": 0.04},
        }[self.quality_profile]
        amount = max(0.0, min(1.0, shot.look_strength * group_strength * profile["look_scale"]))
        effective = apply_look_package(base, self.project_look, amount)
        data = effective.model_dump()
        data["skin_protection"] = max(float(data.get("skin_protection", 0.0)), profile["skin_min"])
        data["gamut_protection"] = max(float(data.get("gamut_protection", 0.0)), profile["gamut_min"])
        data["highlight_rolloff"] = max(float(data.get("highlight_rolloff", 0.0)), profile["rolloff_min"])
        data["highlight_softness"] = max(float(data.get("highlight_softness", 0.0)), profile["softness_min"])
        sources = dict(data.get("parameter_sources") or {})
        confidence = dict(data.get("parameter_confidence") or {})
        for key in ("skin_protection", "gamut_protection", "highlight_rolloff", "highlight_softness"):
            sources[key] = "quality_profile"
            confidence[key] = max(float(confidence.get(key, 0.0)), 0.90 if self.quality_profile == "broadcast_safe" else 0.80)
        data["parameter_sources"] = sources
        data["parameter_confidence"] = confidence
        data["rationales"] = list(data.get("rationales") or []) + [
            f"Quality profile '{self.quality_profile}' applied to creative strength and safety controls."
        ]
        return GradeRecipe.model_validate(data)

    def technical_recipe(self, shot: ShotGrade) -> GradeRecipe:
        """Return only per-shot technical correction and matching layers."""
        base = shot.base_correction
        if shot.shot_match is not None:
            values = base.model_dump()
            match = shot.shot_match
            values["exposure"] += match.exposure
            values["temperature"] += match.temperature
            values["rgb_gains"] = [values["rgb_gains"][i] * match.rgb_gains[i] for i in range(3)]
            values["saturation"] *= match.saturation
            values["contrast"] *= match.contrast
            base = GradeRecipe.model_validate(values)
        return base


def project_from_scene_analysis(source_path: str, scenes: SceneAnalysisResult, look_id: str, scene_analysis_job_id: str = "") -> ProjectGradeRecipe:
    shots = [
        ShotGrade(
            scene_id=scene.scene_id, shot_id=f"shot_{index + 1:04d}", start_time=scene.start_time, end_time=scene.end_time,
            base_correction=(
                GradeRecipe() if scene.grade_decision and scene.grade_decision.action == "preserve"
                else scene.suggested_recipe or GradeRecipe()
            ),
            look_strength=(scene.grade_decision.recommended_look_strength if scene.grade_decision else 1.0),
            base_grade_policy=(
                "preserve" if scene.grade_decision and scene.grade_decision.action == "preserve"
                else "manual_review" if scene.grade_decision and scene.grade_decision.action == "manual_review"
                else "auto"
            ),
            creative_policy=(
                "bypass" if scene.grade_decision and scene.grade_decision.action in {"preserve", "technical_only"}
                else "manual_review" if scene.grade_decision and scene.grade_decision.action == "manual_review"
                else "restrained" if scene.grade_decision and scene.grade_decision.action == "creative_low"
                else "inherit"
            ),
        )
        for index, scene in enumerate(scenes.scenes)
    ]
    groups = [
        SceneGroup(scene_group_id=f"group_{index + 1:04d}", shot_ids=[shot.shot_id], hero_shot_id=shot.shot_id)
        for index, shot in enumerate(shots)
    ]
    return ProjectGradeRecipe(
        source_path=source_path, project_look=look_id, shots=shots, scene_groups=groups,
        scene_analysis_job_id=scene_analysis_job_id,
    )

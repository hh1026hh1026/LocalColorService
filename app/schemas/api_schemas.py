"""
API Pydantic Schemas for Local Color Service REST Endpoints.
"""

from typing import Optional, Dict, Any, List, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from color_core.recipe import GradeRecipe
from color_core.project_recipe import SceneGroup


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "Local Color Service"
    version: str = "0.5.2"
    gpu_available: bool = True
    workers_ready: bool = True
    workers: List[Dict[str, Any]] = Field(default_factory=list)


class VersionResponse(BaseModel):
    service: str = "Local Color Service"
    version: str = "0.5.2"
    ocio_version: str = "2.5.2"
    ocio_config: Optional[str] = "studio-config-v4.0.0_aces-v2.0_ocio-v2.5"
    python_version: str
    ffmpeg_path: str


class AnalyzeRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to video or image file")
    provider_type: Literal["traditional_rule", "reinhard_transfer", "clahe_adaptive", "adaptive_lut_deterministic"] = "traditional_rule"
    reference_path: Optional[str] = Field(default=None, description="Optional reference image path for Reinhard transfer")
    sample_count: int = Field(default=7, ge=1, le=30)


class SceneRequest(BaseModel):
    file_path: str
    detector: Literal["adaptive", "content"] = "adaptive"
    threshold: float = Field(default=3.0, ge=0.1, le=100.0)
    min_scene_len: int = Field(default=12, ge=1, le=300)
    analyze: bool = True


class ProjectRecipeRequest(BaseModel):
    scene_job_id: str
    look_id: Literal[
        "neutral_broadcast", "clean_commercial", "cinematic_soft_print",
        "restrained_teal_amber", "warm_memory", "cold_thriller",
        "sports_vivid", "stage_mixed_light", "day_for_night",
    ] = "neutral_broadcast"
    workflow: Literal["automatic", "professional_assisted", "reference_assisted"] = "professional_assisted"
    quality_profile: Literal["broadcast_safe", "balanced", "creative"] = "balanced"
    auto_group: bool = True
    grouping_threshold: float = Field(default=0.48, ge=0.0, le=2.0)


class ShotGradeUpdate(BaseModel):
    shot_id: str
    exposure: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    temperature: Optional[float] = Field(default=None, ge=-50.0, le=50.0)
    tint: Optional[float] = Field(default=None, ge=-0.5, le=0.5)
    contrast: Optional[float] = Field(default=None, ge=0.5, le=1.5)
    pivot: Optional[float] = Field(default=None, ge=0.01, le=1.0)
    saturation: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    highlight_rolloff: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    highlight_softness: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    color_density: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    skin_protection: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    gamut_protection: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rgb_gains: Optional[List[float]] = None
    lift: Optional[List[float]] = None
    gamma: Optional[List[float]] = None
    gain: Optional[List[float]] = None
    look_strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    enabled: Optional[bool] = None

    @field_validator("rgb_gains", "lift", "gamma", "gain")
    @classmethod
    def validate_grade_vectors(cls, value):
        if value is not None and len(value) != 3:
            raise ValueError("grade vectors must contain exactly 3 values")
        return [float(item) for item in value] if value is not None else value


class GradePlanReviseRequest(BaseModel):
    project_job_id: str
    expected_revision: int = Field(ge=1)
    actor: str = Field(default="local-user", min_length=1, max_length=80)
    project_look: Optional[str] = None
    scene_groups: Optional[List[SceneGroup]] = None
    shot_updates: List[ShotGradeUpdate] = Field(default_factory=list)


class GradePlanApprovalRequest(BaseModel):
    project_job_id: str
    expected_revision: int = Field(ge=1)
    actor: str = Field(default="local-user", min_length=1, max_length=80)


class ProjectRenderRequest(BaseModel):
    project_job_id: str
    input_path: Optional[str] = None
    target_height: int = Field(default=0, ge=0, le=2160)
    preview: bool = False
    allow_unapproved: bool = False
    # delivery = 8-bit H.264 (previous behaviour and still the default).
    # master = 10-bit ProRes 422 HQ / DNxHR for archiving and re-grading.
    output_profile: Literal["preview", "delivery", "master"] = "delivery"
    quality_profile: Optional[Literal["broadcast_safe", "balanced", "creative"]] = None


class TimelinePreviewRequest(BaseModel):
    project_job_id: str
    scope: Literal["shot", "scene_group"] = "shot"
    shot_id: Optional[str] = None
    scene_group_id: Optional[str] = None
    context_seconds: float = Field(default=1.0, ge=0.0, le=5.0)
    target_height: int = Field(default=540, ge=240, le=1080)

    @model_validator(mode="after")
    def validate_scope_target(self):
        if self.scope == "shot" and not self.shot_id:
            raise ValueError("shot_id is required for shot preview")
        if self.scope == "scene_group" and not self.scene_group_id:
            raise ValueError("scene_group_id is required for scene-group preview")
        return self


class TimelineAdjustRequest(BaseModel):
    project_job_id: str
    expected_revision: int = Field(ge=1)
    scope: Literal["shot", "scene_group"] = "shot"
    shot_id: Optional[str] = None
    scene_group_id: Optional[str] = None
    operation: Literal["restore_auto", "match_hero", "auto_repair"]
    repair_category: Optional[Literal[
        "large_exposure_correction", "highlight_clipping", "skin_safety",
        "continuity_luminance", "continuity_white_balance",
    ]] = None
    actor: str = Field(default="local-user", min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_scope_target(self):
        if self.scope == "shot" and not self.shot_id:
            raise ValueError("shot_id is required for shot adjustment")
        if self.scope == "scene_group" and not self.scene_group_id:
            raise ValueError("scene_group_id is required for scene-group adjustment")
        if self.operation == "auto_repair" and not self.repair_category:
            raise ValueError("repair_category is required for automatic repair")
        return self


class QCRepairItem(BaseModel):
    shot_id: str = Field(min_length=1, max_length=120)
    category: Literal[
        "large_exposure_correction", "highlight_clipping", "skin_safety",
        "continuity_luminance", "continuity_white_balance",
    ]


class BatchQCRepairRequest(BaseModel):
    project_job_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    repairs: List[QCRepairItem] = Field(min_length=1, max_length=2000)
    actor: str = Field(default="task-qc-batch-repair", min_length=1, max_length=80)


class ReferencePreflightRequest(BaseModel):
    reference_path: str
    project_job_id: Optional[str] = None
    scene_group_id: Optional[str] = None


class InterchangeExportRequest(BaseModel):
    source_job_id: str
    format: Literal["cc", "ccc", "clf", "cube"]
    scene_id: Optional[str] = None


class AdaIntLutRequest(BaseModel):
    input_path: str
    sample_time: Optional[float] = Field(default=None, ge=0.0)
    checkpoint_path: Optional[str] = None
    lut_size: Literal[33, 65] = 33


class SceneGroupAdaIntRequest(BaseModel):
    project_job_id: str
    expected_revision: int = Field(ge=1)
    scene_group_ids: Optional[List[str]] = None
    lut_size: Literal[33, 65] = 33
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    actor: str = Field(default="local-user", min_length=1, max_length=80)


class ReferenceCandidatesRequest(BaseModel):
    source_path: str
    reference_path: str
    sample_time: Optional[float] = Field(default=None, ge=0.0)
    engine: Literal["statistical", "diffusion", "canoncgt"] = "statistical"
    project_job_id: Optional[str] = None
    scene_group_id: Optional[str] = None
    source_times: Optional[List[float]] = None
    lut_size: Literal[33, 65] = 33
    allow_fallback: bool = True


class CandidateSelectionRequest(BaseModel):
    candidate_job_id: str
    candidate_id: Literal["A", "B", "C"]
    project_job_id: Optional[str] = None
    scene_group_id: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)
    actor: str = Field(default="local-user", min_length=1, max_length=80)


class LutRenderRequest(BaseModel):
    input_path: str
    lut_path: str
    target_height: int = Field(default=720, ge=144, le=2160)
    preview: bool = True
    split_screen: bool = False


class RecipeRequest(BaseModel):
    analysis_job_id: Optional[str] = None
    provider_type: Optional[str] = "traditional_rule"
    exposure: Optional[float] = None
    contrast: Optional[float] = None
    tint: Optional[float] = None
    saturation: Optional[float] = None
    temperature: Optional[float] = None
    highlight_rolloff: Optional[float] = None
    highlight_softness: Optional[float] = None
    color_density: Optional[float] = None
    skin_protection: Optional[float] = None
    gamut_protection: Optional[float] = None
    rgb_gains: Optional[List[float]] = None
    lift: Optional[List[float]] = None
    gamma: Optional[List[float]] = None
    gain: Optional[List[float]] = None
    pivot: Optional[float] = None
    lut_size: Optional[int] = None
    strength: Optional[float] = None
    source_media_hash: Optional[str] = None


class LutRequest(BaseModel):
    recipe: Optional[GradeRecipe] = None
    recipe_job_id: Optional[str] = None
    output_lut_path: Optional[str] = None

    @model_validator(mode="after")
    def validate_recipe_source(self):
        if (self.recipe is None) == (self.recipe_job_id is None):
            raise ValueError("provide exactly one of recipe or recipe_job_id")
        return self


class PreviewRequest(BaseModel):
    input_path: str
    recipe: Optional[GradeRecipe] = None
    recipe_job_id: Optional[str] = None
    target_height: int = Field(default=720, ge=144, le=2160)
    split_screen: Optional[bool] = False

    @model_validator(mode="after")
    def validate_recipe_source(self):
        if (self.recipe is None) == (self.recipe_job_id is None):
            raise ValueError("provide exactly one of recipe or recipe_job_id")
        return self


class RenderRequest(BaseModel):
    input_path: str
    recipe: Optional[GradeRecipe] = None
    recipe_job_id: Optional[str] = None
    output_path: Optional[str] = None

    @model_validator(mode="after")
    def validate_recipe_source(self):
        if (self.recipe is None) == (self.recipe_job_id is None):
            raise ValueError("provide exactly one of recipe or recipe_job_id")
        return self


class JobCreateResponse(BaseModel):
    job_id: str
    job_type: str
    status: str = "pending"
    created_at: str


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: str
    status: str
    progress: int
    result_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str
    # Queue visibility: a pending job is waiting, not broken. Without this the
    # two states are indistinguishable to the caller.
    lane: Optional[str] = None
    queue_position: Optional[int] = None
    blocked_by: Optional[Dict[str, Any]] = None


class ComparisonMediaItem(BaseModel):
    id: str
    label: str
    url: str
    kind: Literal["source", "preview", "render"]
    filename: str
    size_bytes: int
    modified_at: str
    group_key: str
    job_id: Optional[str] = None
    recipe_summary: Optional[str] = None


class ComparisonMediaResponse(BaseModel):
    items: List[ComparisonMediaItem]

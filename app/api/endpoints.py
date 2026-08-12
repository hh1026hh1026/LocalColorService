"""FastAPI endpoints for the V0.5 local color workflow."""

import sys
import os
import json
import time
import uuid
import hashlib
import subprocess
import mimetypes
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.schemas.api_schemas import (
    HealthResponse, VersionResponse,
    AnalyzeRequest, RecipeRequest, LutRequest, PreviewRequest, RenderRequest,
    JobCreateResponse, JobStatusResponse, ComparisonMediaResponse, SceneRequest,
    ProjectRecipeRequest, ProjectRenderRequest, InterchangeExportRequest,
    TimelinePreviewRequest, TimelineAdjustRequest, ReferencePreflightRequest,
    BatchQCRepairRequest,
    AdaIntLutRequest, ReferenceCandidatesRequest, CandidateSelectionRequest,
    SceneGroupAdaIntRequest,
    LutRenderRequest,
    GradePlanReviseRequest, GradePlanApprovalRequest,
)
from color_core.recipe import GradeRecipe
from color_core.plugin_interface import suggestion_registry
from color_core.ocio_manager import OCIOManager
from color_core.renderer import get_ffmpeg_executable
from app.services.database import db_manager
from app.services.job_store import initialize_job
from app.services.logger import logger, get_recent_logs
from app.services.comparison_media import build_comparison_inventory, resolve_job_source
from app.config import settings
from color_core.look_packages import list_look_packages
from color_core.neural_models import neural_runtime_status
from color_core.project_recipe import ProjectGradeRecipe
from color_core.artifact_freeze import verify_approval_bundle

router = APIRouter()
ocio_manager = OCIOManager()

UPLOAD_DIR = settings.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _task_execution_details(job: dict) -> dict:
    """Compact, operator-facing explanation of how a queued job will run."""
    params = job.pop("input_params", {}) or {}
    job_type = job["job_type"]
    model_by_type = {
        "adaint_lut": "AdaInt / AiLUT-FiveK", "scene_group_adaint": "AdaInt / AiLUT-FiveK",
        "reference_candidates": "CanonCGT" if params.get("engine") == "canoncgt" else "Reference candidate engine",
        "project_render": "FFmpeg timeline + OCIO/LUT", "timeline_preview": "FFmpeg timeline + OCIO/LUT",
        "qc_auto_repair": "GradePlan QC 批量修复",
        "lut_render": "FFmpeg + 3D LUT", "scenes": "PySceneDetect + frame analysis",
        "analyze": "Image analysis provider",
    }
    mode = params.get("provider_type") or params.get("engine") or params.get("workflow")
    if job_type == "project_render":
        mode = ("preview" if params.get("preview") else "formal render") + f" · {params.get('output_profile', 'delivery')}"
    elif job_type == "timeline_preview":
        mode = params.get("scope", "shot") + " preview"
    elif not mode:
        mode = job_type.replace("_", " ")
    source = params.get("input_path") or params.get("source_path") or params.get("reference_path")
    return {
        **job,
        "execution": {
            "mode": str(mode),
            "model": model_by_type.get(job_type, "Local Color pipeline"),
            "source_name": Path(source).name if source else None,
            "target_height": params.get("target_height"),
            "quality_profile": params.get("quality_profile"),
        },
    }


def _job_detail_payload(job: dict) -> dict:
    """Build a bounded detail payload suitable for the task manager UI."""
    result = job.get("result_data") or {}
    execution = _task_execution_details({**job, "input_params": job.get("input_params") or {}})["execution"]
    recipe = result.get("project_recipe") or {}
    if recipe.get("quality_profile"):
        execution["quality_profile"] = recipe.get("quality_profile")
    output_path = result.get("output_path") or result.get("preview_path")
    output_url = None
    if output_path:
        output = Path(output_path).resolve()
        root = (settings.DATA_DIR / "jobs" / job["job_id"]).resolve()
        try:
            output_url = f"/v1/jobs/{job['job_id']}/artifacts/{output.relative_to(root).as_posix()}"
        except ValueError:
            output_url = None
    project_qc = result.get("project_quality_report") or {}
    project_model = None
    if recipe:
        try:
            project_model = ProjectGradeRecipe.model_validate(recipe)
        except Exception as exc:
            # A malformed/legacy recipe must not make the task board unusable.
            # The raw recipe is still returned and the per-shot effective view
            # will fall back to the stored base correction below.
            logger.warning(f"Task detail recipe validation fallback for [{job['job_id']}]: {exc}")
    shots = []
    for index, shot in enumerate(recipe.get("shots") or []):
        correction = shot.get("base_correction") or {}
        effective = {}
        if project_model and index < len(project_model.shots):
            try:
                effective_recipe = project_model.effective_recipe(project_model.shots[index]).model_dump(mode="json")
                effective = {key: effective_recipe.get(key) for key in (
                    "exposure", "temperature", "tint", "contrast", "pivot", "saturation",
                    "highlight_rolloff", "highlight_softness", "color_density", "skin_protection",
                    "gamut_protection", "rgb_gains", "lift", "gamma", "gain", "look_id",
                    "parameter_sources", "parameter_confidence",
                )}
            except Exception as exc:
                logger.warning(f"Task detail effective recipe fallback for [{job['job_id']}] shot index {index}: {exc}")
        shots.append({
            "shot_id": shot.get("shot_id") or shot.get("scene_id"),
            "scene_id": shot.get("scene_id"), "start_time": shot.get("start_time", 0),
            "end_time": shot.get("end_time", 0), "enabled": shot.get("enabled", True),
            "exposure": correction.get("exposure", 0), "temperature": correction.get("temperature", 0),
            "tint": correction.get("tint", 0), "contrast": correction.get("contrast", 1),
            "pivot": correction.get("pivot", 0.18), "saturation": correction.get("saturation", 1),
            "highlight_rolloff": correction.get("highlight_rolloff", 0),
            "highlight_softness": correction.get("highlight_softness", 0),
            "color_density": correction.get("color_density", 0),
            "skin_protection": correction.get("skin_protection", 0),
            "gamut_protection": correction.get("gamut_protection", 0),
            "rgb_gains": correction.get("rgb_gains", [1, 1, 1]),
            "lift": correction.get("lift", [0, 0, 0]),
            "gamma": correction.get("gamma", [1, 1, 1]),
            "gain": correction.get("gain", [1, 1, 1]),
            "look_id": correction.get("look_id", "neutral_broadcast"),
            "parameter_sources": correction.get("parameter_sources", {}),
            "parameter_confidence": correction.get("parameter_confidence", {}),
            "look_strength": shot.get("look_strength", correction.get("look_strength", 0)),
            "effective": effective,
        })
    review_items = project_qc.get("review_items") or []
    categories: dict[str, int] = {}
    for item in review_items:
        category = item.get("category", "other")
        categories[category] = categories.get(category, 0) + 1
    return {
        "job_id": job["job_id"], "job_type": job["job_type"], "status": job["status"],
        "progress": job["progress"], "error_message": job.get("error_message"),
        "created_at": job["created_at"], "updated_at": job["updated_at"],
        "execution": execution, "project_job_id": (job.get("input_params") or {}).get("project_job_id"),
        "execution": execution, "output_path": output_path, "output_url": output_url,
        "result_summary": {
            "revision": recipe.get("revision"), "workflow": recipe.get("workflow"),
            "quality_profile": recipe.get("quality_profile"),
            "shot_count": len(recipe.get("shots") or []), "scene_group_count": len(recipe.get("scene_groups") or []),
            "face_selective_render": result.get("face_selective_render"),
            "safety_fallback_count": len(result.get("safety_fallbacks") or []),
            "repair_count": result.get("repair_count"),
            "updated_shot_count": len(result.get("updated_shots") or []),
            "repair_categories": sorted({item.get("category") for item in result.get("repairs") or [] if item.get("category")}),
        },
        "project_recipe": {
            "revision": recipe.get("revision"), "status": recipe.get("status"),
            "source_path": recipe.get("source_path"), "shots": shots,
        } if recipe else None,
        "quality_report": result.get("quality_report"),
        "project_quality_report": project_qc or None,
        "review_category_counts": categories,
        "artifacts": result.get("artifacts") or {},
    }


def _create_job(job_type: str, input_params: dict) -> JobCreateResponse:
    job_id = uuid.uuid4().hex[:12]
    job_info = db_manager.create_job(job_id, job_type, input_params)
    try:
        initialize_job(job_id, {"job_id": job_id, "job_type": job_type, **input_params})
    except Exception:
        db_manager.delete_job(job_id)
        raise
    return JobCreateResponse(job_id=job_id, job_type=job_type, status="pending", created_at=job_info["created_at"])


class TransferRequest(BaseModel):
    source_path: str
    reference_path: str


@router.get("/v1/providers", tags=["System"])
def list_providers():
    """
    Returns list of all registered color algorithm and model providers.
    """
    return {
        "status": "success",
        "providers": suggestion_registry.list_providers()
    }


@router.get("/v1/looks", tags=["Color V0.5"])
def list_looks():
    return {"status": "success", "looks": list_look_packages()}


@router.get("/v1/models/status", tags=["Color V0.5"])
def get_model_status():
    return {"status": "success", "models": neural_runtime_status()}


@router.get("/v1/performance/status", tags=["System"])
def get_performance_status():
    ffmpeg = get_ffmpeg_executable()
    def output(flag: str) -> str:
        result = subprocess.run([ffmpeg, "-hide_banner", flag], capture_output=True, text=True, timeout=30, check=False)
        return (result.stdout or "") + (result.stderr or "")
    encoders, decoders, filters = output("-encoders"), output("-decoders"), output("-filters")
    return {
        "status": "success",
        "ffmpeg": ffmpeg,
        "acceleration": {
            "nvenc_preview": "h264_nvenc" in encoders,
            "nvdec_h264": "h264_cuvid" in decoders,
            "nvdec_hevc": "hevc_cuvid" in decoders,
            "cuda_scale": "scale_cuda" in filters,
            "gpu_lut3d": "libplacebo" in filters,
            "lut_backend": "GPU" if "libplacebo" in filters else "CPU tetrahedral",
        },
    }


@router.post("/v1/color/transfer", response_model=JobCreateResponse, tags=["Color"])
def create_transfer_job(req: TransferRequest):
    if not os.path.exists(req.source_path):
        raise HTTPException(status_code=404, detail=f"Source file not found: {req.source_path}")
    if not os.path.exists(req.reference_path):
        raise HTTPException(status_code=404, detail=f"Reference file not found: {req.reference_path}")

    input_params = req.model_dump(exclude_none=True)
    return _create_job("transfer", input_params)


@router.get("/v1/system/logs", tags=["System"])
def get_system_logs(
    level: Optional[str] = Query(None, description="Log level filter: INFO, WARNING, ERROR"),
    limit: int = Query(100, ge=1, le=500, description="Max log lines to return")
):
    return {
        "status": "success",
        "logs": get_recent_logs(level=level, limit=limit)
    }


@router.get("/v1/comparison/media", response_model=ComparisonMediaResponse, tags=["Comparison"])
def list_comparison_media():
    """List browser-playable originals, previews, and final renders."""
    return ComparisonMediaResponse(items=build_comparison_inventory(settings.DATA_DIR))


@router.get("/v1/comparison/jobs/{job_id}/source", tags=["Comparison"])
def stream_comparison_source(job_id: str):
    """Serve only the source media recorded by an existing comparison job."""
    source = resolve_job_source(settings.DATA_DIR, job_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Playable source media not found for this job")
    media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return FileResponse(source, media_type=media_type, filename=source.name, content_disposition_type="inline")


@router.post("/v1/upload", tags=["System"])
async def upload_file(file: UploadFile = File(...)):
    file_id = uuid.uuid4().hex[:8]
    ext = Path(file.filename).suffix or ".mp4"
    temp_path = UPLOAD_DIR / f".upload_{file_id}.part"

    try:
        digest = hashlib.sha256()
        size = 0
        with temp_path.open("wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 10 * 1024 * 1024 * 1024:
                    raise ValueError("Upload exceeds the 10 GB local-service limit")
                digest.update(chunk)
                buffer.write(chunk)
        dest_filename = f"{digest.hexdigest()[:24]}{ext.lower()}"
        dest_path = UPLOAD_DIR / dest_filename
        if dest_path.exists():
            temp_path.unlink()
            logger.info(f"Upload deduplicated: {file.filename} -> {dest_path}")
        else:
            temp_path.replace(dest_path)
            logger.info(f"File uploaded successfully: {file.filename} -> {dest_path}")
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        logger.error(f"Failed to save uploaded file: {e}")
        status = 413 if isinstance(e, ValueError) else 500
        raise HTTPException(status_code=status, detail=f"Failed to save uploaded file: {e}")

    return {
        "status": "success",
        "filename": dest_filename,
        "file_path": str(dest_path.resolve())
    }


@router.get("/health", response_model=HealthResponse, tags=["System"])
def get_health(request: Request):
    gpu_available = False
    try:
        res = subprocess.run(["nvidia-smi"], capture_output=True)
        gpu_available = res.returncode == 0
    except Exception:
        pass

    workers = [worker.status_snapshot() for worker in getattr(request.app.state, "job_workers", [])]
    workers_ready = bool(workers) and all(worker["alive"] for worker in workers)
    return HealthResponse(
        status="ok" if workers_ready else "degraded",
        service="Local Color Service",
        version=settings.VERSION,
        gpu_available=gpu_available,
        workers_ready=workers_ready,
        workers=workers,
    )


@router.get("/version", response_model=VersionResponse, tags=["System"])
def get_version():
    try:
        import PyOpenColorIO as ocio
        ocio_ver = ocio.__version__
    except Exception:
        ocio_ver = "Unknown"

    return VersionResponse(
        service="Local Color Service",
        version=settings.VERSION,
        ocio_version=ocio_ver,
        ocio_config=ocio_manager.config_id,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ffmpeg_path=get_ffmpeg_executable()
    )


@router.post("/v1/color/analyze", response_model=JobCreateResponse, tags=["Color"])
def create_analyze_job(req: AnalyzeRequest):
    if not os.path.exists(req.file_path):
        logger.error(f"Analyze request file not found: {req.file_path}")
        raise HTTPException(status_code=404, detail=f"Source file not found: {req.file_path}")
    if req.reference_path and not os.path.exists(req.reference_path):
        raise HTTPException(status_code=404, detail=f"Reference file not found: {req.reference_path}")

    input_params = req.model_dump(exclude_none=True)
    response = _create_job("analyze", input_params)
    logger.info(f"Created analyze job [{response.job_id}] for file: {req.file_path} with provider={req.provider_type}")
    return response


@router.post("/v1/color/scenes", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_scene_job(req: SceneRequest):
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=404, detail=f"Source file not found: {req.file_path}")
    return _create_job("scenes", req.model_dump(exclude_none=True))


@router.post("/v1/color/project-recipe", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_project_recipe_job(req: ProjectRecipeRequest):
    return _create_job("project_recipe", req.model_dump(exclude_none=True))


@router.post("/v1/color/grade-plan/revise", response_model=JobCreateResponse, tags=["Color V0.3"])
def revise_grade_plan(req: GradePlanReviseRequest):
    return _create_job("grade_plan_revise", req.model_dump(exclude_none=True))


@router.post("/v1/color/grade-plan/approve", response_model=JobCreateResponse, tags=["Color V0.3"])
def approve_grade_plan(req: GradePlanApprovalRequest):
    return _create_job("grade_plan_approve", req.model_dump(exclude_none=True))


@router.get("/v1/color/approval-bundle/verify", tags=["Color V0.4"])
def verify_grade_plan_bundle(project_job_id: str = Query(..., min_length=1)):
    job = db_manager.get_job(project_job_id)
    project_data = ((job or {}).get("result_data") or {}).get("project_recipe") if job else None
    if not project_data:
        raise HTTPException(status_code=404, detail=f"Approved GradePlan not found: {project_job_id}")
    project = ProjectGradeRecipe.model_validate(project_data)
    if not project.approval_bundle_path:
        raise HTTPException(status_code=409, detail="GradePlan has no frozen approval bundle")
    return verify_approval_bundle(project.approval_bundle_path)


@router.post("/v1/color/project-render", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_project_render_job(req: ProjectRenderRequest):
    if req.input_path and not os.path.exists(req.input_path):
        raise HTTPException(status_code=404, detail=f"Source file not found: {req.input_path}")
    return _create_job("project_render", req.model_dump(exclude_none=True))


@router.post("/v1/color/timeline-preview", response_model=JobCreateResponse, tags=["Color V0.5.1"])
def create_timeline_preview_job(req: TimelinePreviewRequest):
    return _create_job("timeline_preview", req.model_dump(exclude_none=True))


@router.post("/v1/color/timeline-adjust", response_model=JobCreateResponse, tags=["Color V0.5.1"])
def create_timeline_adjust_job(req: TimelineAdjustRequest):
    return _create_job("timeline_adjust", req.model_dump(exclude_none=True))


@router.post("/v1/color/qc-auto-repair", response_model=JobCreateResponse, tags=["Color V0.6"])
def create_batch_qc_repair_job(req: BatchQCRepairRequest):
    """Apply all selected QC repairs in one GradePlan revision."""
    return _create_job("qc_auto_repair", req.model_dump(exclude_none=True))


@router.post("/v1/color/export", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_export_job(req: InterchangeExportRequest):
    return _create_job("export", req.model_dump(exclude_none=True))


@router.post("/v1/color/adaint-lut", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_adaint_job(req: AdaIntLutRequest):
    if not os.path.exists(req.input_path):
        raise HTTPException(status_code=404, detail=f"Source file not found: {req.input_path}")
    return _create_job("adaint_lut", req.model_dump(exclude_none=True))


@router.post("/v1/color/scene-group-adaint", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_scene_group_adaint_job(req: SceneGroupAdaIntRequest):
    return _create_job("scene_group_adaint", req.model_dump(exclude_none=True))


@router.post("/v1/color/reference-candidates", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_reference_candidates_job(req: ReferenceCandidatesRequest):
    for path in (req.source_path, req.reference_path):
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"Media file not found: {path}")
    return _create_job("reference_candidates", req.model_dump(exclude_none=True))


@router.post("/v1/color/reference-preflight", response_model=JobCreateResponse, tags=["Color V0.5.1"])
def create_reference_preflight_job(req: ReferencePreflightRequest):
    if not os.path.exists(req.reference_path):
        raise HTTPException(status_code=404, detail=f"Reference file not found: {req.reference_path}")
    return _create_job("reference_preflight", req.model_dump(exclude_none=True))


@router.post("/v1/color/reference-select", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_candidate_selection_job(req: CandidateSelectionRequest):
    return _create_job("candidate_select", req.model_dump(exclude_none=True))


@router.post("/v1/color/lut-render", response_model=JobCreateResponse, tags=["Color V0.5"])
def create_lut_render_job(req: LutRenderRequest):
    if not os.path.exists(req.input_path):
        raise HTTPException(status_code=404, detail=f"Input file not found: {req.input_path}")
    if not os.path.exists(req.lut_path):
        raise HTTPException(status_code=404, detail=f"LUT file not found: {req.lut_path}")
    return _create_job("lut_render", req.model_dump(exclude_none=True))


@router.post("/v1/color/recipe", response_model=JobCreateResponse, tags=["Color"])
def create_recipe(req: RecipeRequest):
    input_params = req.model_dump(exclude_none=True)
    return _create_job("recipe", input_params)


@router.post("/v1/color/lut", response_model=JobCreateResponse, tags=["Color"])
def create_lut_job(req: LutRequest):
    input_params = req.model_dump(exclude_none=True)
    return _create_job("lut", input_params)


@router.post("/v1/color/preview", response_model=JobCreateResponse, tags=["Color"])
def create_preview_job(req: PreviewRequest):
    if not os.path.exists(req.input_path):
        logger.error(f"Preview request file not found: {req.input_path}")
        raise HTTPException(status_code=404, detail=f"Input file not found: {req.input_path}")

    input_params = req.model_dump(exclude_none=True)
    return _create_job("preview", input_params)


@router.post("/v1/color/render", response_model=JobCreateResponse, tags=["Color"])
def create_render_job(req: RenderRequest):
    if not os.path.exists(req.input_path):
        logger.error(f"Render request file not found: {req.input_path}")
        raise HTTPException(status_code=404, detail=f"Input file not found: {req.input_path}")

    input_params = req.model_dump(exclude_none=True)
    return _create_job("render", input_params)


@router.get("/v1/jobs/{job_id}", response_model=JobStatusResponse, tags=["Jobs"])
def get_job_status(job_id: str):
    job = db_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    queue = db_manager.queue_snapshot(job_id)
    return JobStatusResponse(
        job_id=job["job_id"],
        job_type=job["job_type"],
        status=job["status"],
        progress=job["progress"],
        result_data=job["result_data"],
        error_message=job["error_message"],
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        lane=queue.get("lane"),
        queue_position=queue.get("queue_position"),
        blocked_by=queue.get("blocked_by"),
    )


@router.get("/v1/jobs", tags=["Jobs"])
def list_jobs(
    statuses: Optional[str] = Query(None, description="Comma-separated job statuses"),
    limit: int = Query(100, ge=1, le=500),
):
    """Task-board feed. Result payloads are intentionally omitted."""
    requested = [value.strip() for value in (statuses or "").split(",") if value.strip()]
    jobs = db_manager.list_jobs(requested or None, limit)
    for job in jobs:
        queue = db_manager.queue_snapshot(job["job_id"])
        job["lane"] = queue.get("lane")
        job["queue_position"] = queue.get("queue_position")
    jobs = [_task_execution_details(job) for job in jobs]
    return {"jobs": jobs}


@router.get("/v1/jobs/{job_id}/detail", tags=["Jobs"])
def get_job_detail(job_id: str):
    job = db_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_detail_payload(job)


@router.delete("/v1/jobs/{job_id}", tags=["Jobs"])
def cancel_job(job_id: str, purge: bool = Query(False, description="Also delete the record and its files")):
    """Cancel a job, optionally deleting it.

    A pending job is settled immediately. A running one has its FFmpeg processes
    terminated, so a fifteen-minute render started by mistake no longer has to
    run to completion or take a service restart (and the whole queue) with it.
    """
    outcome = db_manager.cancel_job(job_id)
    if not outcome.get("found"):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    logger.info(f"Cancel requested for job [{job_id}]: {outcome}")
    if purge:
        # Give the worker a moment to record the cancellation before removing it.
        for _ in range(20):
            current = db_manager.get_job(job_id)
            if not current or current["status"] not in ("processing",):
                break
            time.sleep(0.25)
        outcome["purged"] = db_manager.purge_job(job_id)
    return {"status": "success", **outcome}


@router.post("/v1/jobs/cancel-all", tags=["Jobs"])
def cancel_all_jobs():
    """Cancel everything pending or running."""
    outcome = db_manager.cancel_all_active()
    logger.warning(f"Cancel-all requested: {len(outcome['requested'])} job(s)")
    return {"status": "success", **outcome}


class JobPurgeRequest(BaseModel):
    statuses: List[str] = ["completed", "failed", "cancelled"]
    older_than_days: Optional[float] = None


@router.post("/v1/jobs/purge", tags=["Jobs"])
def purge_jobs(req: JobPurgeRequest):
    """Delete finished job records and their directories.

    Active jobs are refused; cancel them first.
    """
    try:
        result = db_manager.purge_jobs(req.statuses, req.older_than_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.warning(
        f"Purged {result['purged']} job(s), freed {result['freed_bytes'] / 1e9:.2f} GB"
    )
    return {"status": "success", **result}


@router.get("/v1/jobs/{job_id}/artifacts/{artifact_path:path}", tags=["Jobs"])
def stream_job_artifact(job_id: str, artifact_path: str):
    """Serve a file only when it resolves inside the requested job directory."""
    root = (settings.DATA_DIR / "jobs" / job_id).resolve()
    target = (root / artifact_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid artifact path") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name, content_disposition_type="inline")


@router.get("/v1/jobs/{job_id}/report", tags=["Jobs"])
def get_job_qc_report(job_id: str):
    job = db_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job["status"] != "completed":
        return {
            "job_id": job_id,
            "status": job["status"],
            "message": f"Job is currently {job['status']}"
        }

    res_data = job.get("result_data", {})
    if "qc_report" in res_data:
        return res_data["qc_report"]
    elif "analysis_report" in res_data:
        return res_data["analysis_report"]
    elif "artifacts" in res_data:
        return res_data
    else:
        return {"job_id": job_id, "result": res_data}

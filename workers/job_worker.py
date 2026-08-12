"""Single local SQLite-backed job worker."""

from __future__ import annotations

import cv2
import hashlib
import json
import math
import os
import threading
import time
import traceback
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.services.database import db_manager
from app.services.job_store import append_event, append_log, artifact_map, job_directory, write_json
from app.services.logger import logger
from color_core.cancellation import JobCancelled, raise_if_cancelled, release, request_cancel, set_current_job
from color_core.degradation import report_degradation
from color_core.frame_sampler import sample_frames
from color_core.image_analyzer import analyze_image_frames
from color_core.lut_baker import bake_3d_lut
from color_core.media_probe import probe_media
from color_core.ocio_manager import OCIOManager
from color_core.plugin_interface import suggestion_registry
from color_core.quality_control import perform_quality_control
from color_core.recipe import GradeRecipe
from color_core.renderer import render_final, render_preview
from color_core.scene_analysis import SceneAnalysisResult, detect_and_analyze_scenes
from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, TransformAsset, project_from_scene_analysis
from color_core.project_qc import evaluate_project_quality, inspect_cube_lut
from color_core.artifact_freeze import freeze_approved_revision, verify_approval_bundle
from color_core.provenance import compute_file_sha256
from color_core.scene_grouping import suggest_scene_groups
from color_core.group_white_balance import harmonize_group_white_balance
from color_core.timeline_renderer import render_shot_timeline
from color_core.interchange import export_cc, export_ccc, export_clf
from color_core.neural_models import generate_adaint_lut, run_reference_lut_model
from color_core.reference_candidates import generate_reference_candidates
from color_core.canoncgt_provider import CanonCGTProvider, inspect_reference_frame
from color_core.selective_renderer import render_face_selective_timeline
from color_core.output_profiles import container_suffix, video_encoder_args
from color_core.renderer import get_ffmpeg_executable
from color_core.cube_tools import apply_cube_to_bgr, compose_cube_luts, scale_cube_strength


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Interactive job types: sub-second to a few seconds, and the user is normally
# staring at the UI waiting for them. They get their own worker so they never
# queue behind a render.
#
# This was not a theoretical concern: a reference preflight for a CanonCGT
# reference image sat in 'pending' indefinitely because a face-selective
# project_render had been running for fifteen minutes on the single queue, and
# from the UI it was indistinguishable from a failed upload.
LIGHT_JOB_TYPES: tuple[str, ...] = (
    "reference_preflight",
    "project_recipe",
    "grade_plan_revise",
    "grade_plan_approve",
    "timeline_adjust",
    "qc_auto_repair",
    "candidate_select",
    "export",
    "recipe",
    "lut",
)


def lane_for_job_type(job_type: str) -> str:
    """Anything not explicitly known to be quick is treated as heavy."""
    return "light" if job_type in LIGHT_JOB_TYPES else "heavy"


class JobWorker(threading.Thread):
    def __init__(self, poll_interval: float = 0.2, lane: str = "heavy"):
        if lane not in ("heavy", "light"):
            raise ValueError(f"Unknown worker lane: {lane}")
        super().__init__(name=f"LocalColorJobWorker-{lane}", daemon=True)
        self.poll_interval = poll_interval
        self.lane = lane
        self.stop_event = threading.Event()
        self.ocio = OCIOManager()
        self.worker_id = f"{lane}-{uuid.uuid4().hex}"
        self._state_lock = threading.RLock()
        self._current_job_id: str | None = None
        self._last_error: str | None = None
        self._last_heartbeat = time.time()

    def run(self) -> None:
        logger.info(f"LocalColorJobWorker[{self.lane}] background thread started.")
        # Only the heavy lane recovers interrupted jobs. Running recovery from
        # both threads could requeue a job the other lane has just claimed.
        if self.lane == "heavy":
            recovery = db_manager.recover_interrupted_jobs()
            if recovery["requeued"] or recovery["failed"]:
                logger.warning(f"Interrupted job recovery: {recovery}")
        while not self.stop_event.is_set():
            try:
                if self.lane == "light":
                    job = db_manager.fetch_next_pending_job(include_types=list(LIGHT_JOB_TYPES), worker_id=self.worker_id)
                else:
                    job = db_manager.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES), worker_id=self.worker_id)
                if job:
                    self._process_job(job)
                else:
                    self.stop_event.wait(self.poll_interval)
            except Exception as exc:  # A worker must remain observable and recoverable after infrastructure faults.
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(f"LocalColorJobWorker[{self.lane}] loop error: {exc}")
                self.stop_event.wait(max(1.0, self.poll_interval))

    def stop(self) -> None:
        logger.info(f"Stopping LocalColorJobWorker[{self.lane}] thread...")
        self.stop_event.set()
        with self._state_lock:
            job_id = self._current_job_id
        if job_id:
            db_manager.requeue_owned_job(job_id, self.worker_id, "Worker stopped during controlled shutdown")
            request_cancel(job_id)

    def status_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "lane": self.lane,
                "worker_id": self.worker_id,
                "alive": self.is_alive(),
                "current_job_id": self._current_job_id,
                "last_error": self._last_error,
                "last_heartbeat": self._last_heartbeat,
            }

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id, job_type = job["job_id"], job["job_type"]
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(5.0):
                if not db_manager.heartbeat_job(job_id, self.worker_id):
                    return
                with self._state_lock:
                    self._last_heartbeat = time.time()

        with self._state_lock:
            self._current_job_id = job_id
            self._last_heartbeat = time.time()
            self._last_error = None
        heartbeat_thread = threading.Thread(target=heartbeat, name=f"LocalColorLease-{self.lane}", daemon=True)
        heartbeat_thread.start()
        append_log(job_id, f"processing {job_type}")
        append_event(job_id, "job.processing", {"job_type": job_type})
        logger.info(f"Start processing job [{job_id}] type='{job_type}'")
        set_current_job(job_id)
        try:
            handler = getattr(self, f"_handle_{job_type}")
            result = handler(job_id, job.get("input_params") or {})
            result["artifacts"] = artifact_map(job_id)
            db_manager.update_job_status(job_id, "completed", 100, result_data=result, worker_id=self.worker_id)
            append_log(job_id, "completed")
            append_event(job_id, "job.completed", {"job_type": job_type, "artifact_count": len(result.get("artifacts", {}))})
            logger.info(f"Successfully completed job [{job_id}] type='{job_type}'")
        except JobCancelled:
            # A cancellation is a user decision, not a defect; it must not be
            # recorded as a failure or it pollutes the failure history.
            append_log(job_id, "cancelled")
            append_event(job_id, "job.cancelled", {"job_type": job_type})
            logger.info(f"Job [{job_id}] type='{job_type}' cancelled by request")
            db_manager.update_job_status(
                job_id, "cancelled", 100, error_message="Cancelled by request", worker_id=self.worker_id
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            append_log(job_id, "failed: " + message)
            append_event(job_id, "job.failed", {"job_type": job_type, "error_type": type(exc).__name__, "error": str(exc)})
            logger.error(f"Job [{job_id}] type='{job_type}' FAILED: {exc}\n{traceback.format_exc()}")
            self._last_error = f"{type(exc).__name__}: {exc}"
            db_manager.update_job_status(job_id, "failed", 100, error_message=message, worker_id=self.worker_id)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)
            release(job_id)
            set_current_job(None)
            with self._state_lock:
                self._current_job_id = None

    def _complete(self, recipe: GradeRecipe) -> GradeRecipe:
        return self.ocio.complete_recipe(recipe)

    def _apply_shot_match(
        self, project: ProjectGradeRecipe, analysis: SceneAnalysisResult, target_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate bounded exposure/WB/saturation trims from measured shot statistics."""
        scenes = {item.scene_id: item for item in analysis.scenes}
        diagnostics: list[dict[str, Any]] = []
        for group in project.scene_groups:
            hero = next((item for item in project.shots if item.shot_id == group.hero_shot_id), None)
            hero_scene = scenes.get(hero.scene_id) if hero else None
            if not hero or not hero_scene or not hero_scene.analysis:
                continue
            hero_report = hero_scene.analysis
            hero_lum = max(0.01, hero_report.luminance.median * (2.0 ** hero.base_correction.exposure))
            hero_temp = hero_report.white_balance.estimated_temp_offset * 50.0 + hero.base_correction.temperature
            hero_channels = [
                hero_report.channel.r_mean * hero.base_correction.rgb_gains[0],
                hero_report.channel.g_mean * hero.base_correction.rgb_gains[1],
                hero_report.channel.b_mean * hero.base_correction.rgb_gains[2],
            ]
            for shot in project.shots:
                if shot.shot_id not in group.shot_ids or (target_ids is not None and shot.shot_id not in target_ids):
                    continue
                if shot is hero:
                    shot.shot_match = None
                    continue
                scene = scenes.get(shot.scene_id)
                if not scene or not scene.analysis or shot.base_grade_policy != "auto":
                    shot.shot_match = None
                    continue
                report = scene.analysis
                current_lum = max(0.01, report.luminance.median * (2.0 ** shot.base_correction.exposure))
                exposure = max(-0.35, min(0.35, math.log2(hero_lum / current_lum)))
                current_temp = report.white_balance.estimated_temp_offset * 50.0 + shot.base_correction.temperature
                temperature = max(-10.0, min(10.0, hero_temp - current_temp))
                current_channels = [
                    report.channel.r_mean * shot.base_correction.rgb_gains[0],
                    report.channel.g_mean * shot.base_correction.rgb_gains[1],
                    report.channel.b_mean * shot.base_correction.rgb_gains[2],
                ]
                ratios = [hero_channels[i] / max(current_channels[i], 0.01) for i in range(3)]
                neutralizer = max(ratios[1], 0.01)
                rgb_gains = [max(0.96, min(1.04, value / neutralizer)) for value in ratios]
                hero_sat = hero_report.saturation.mean * hero.base_correction.saturation
                current_sat = max(0.01, report.saturation.mean * shot.base_correction.saturation)
                saturation = max(0.92, min(1.08, hero_sat / current_sat))
                shot.shot_match = self._complete(GradeRecipe(
                    exposure=round(exposure, 4), temperature=round(temperature, 4),
                    rgb_gains=[round(value, 4) for value in rgb_gains], saturation=round(saturation, 4),
                    rationales=[f"bounded statistical match to hero shot {hero.shot_id}"],
                ))
                diagnostics.append({
                    "shot_id": shot.shot_id, "hero_shot_id": hero.shot_id,
                    "exposure_trim": round(exposure, 4), "temperature_trim": round(temperature, 4),
                    "rgb_gains": [round(value, 4) for value in rgb_gains], "saturation": round(saturation, 4),
                })
        return diagnostics

    @staticmethod
    def _completed_job(job_id: str, label: str = "Source") -> dict[str, Any]:
        source = db_manager.get_job(job_id)
        if not source or source["status"] != "completed":
            raise ValueError(f"{label} job is not completed: {job_id}")
        return source

    def _recipe_from_params(self, params: dict[str, Any]) -> GradeRecipe:
        if params.get("recipe") is not None:
            return self._complete(GradeRecipe.model_validate(params["recipe"]))
        source_id = params.get("recipe_job_id")
        if not source_id:
            raise ValueError("Missing recipe source")
        source = db_manager.get_job(source_id)
        if not source or source["status"] != "completed":
            raise ValueError(f"Recipe job is not completed: {source_id}")
        recipe_data = (source.get("result_data") or {}).get("recipe")
        if not recipe_data:
            recipe_file = job_directory(source_id) / "grade_recipe.json"
            if recipe_file.is_file():
                import json
                recipe_data = json.loads(recipe_file.read_text(encoding="utf-8"))
        if not recipe_data:
            raise ValueError(f"Job has no grade recipe: {source_id}")
        return self._complete(GradeRecipe.model_validate(recipe_data))

    def _project_from_job(self, job_id: str, label: str = "GradePlan") -> ProjectGradeRecipe:
        source = self._completed_job(job_id, label)
        project_data = (source.get("result_data") or {}).get("project_recipe")
        if not project_data:
            project_file = job_directory(job_id) / "project_recipe.json"
            if project_file.is_file():
                import json
                project_data = json.loads(project_file.read_text(encoding="utf-8"))
        if not project_data:
            raise ValueError(f"Job has no GradePlan: {job_id}")
        return ProjectGradeRecipe.model_validate(project_data)

    def _handle_analyze(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        source = params["file_path"]
        provider_type = params.get("provider_type", "traditional_rule")
        logger.info(f"Analyzing media file: {source} with provider: {provider_type}")
        
        media = probe_media(source)
        frames = sample_frames(source, params.get("sample_count", 7), media.duration)
        analysis = analyze_image_frames(frames)
        
        provider = suggestion_registry.get(provider_type)
        context = {
            "source_hash": sha256_file(source),
            "lut_size": params.get("lut_size", 33),
            "source_frame": frames[0],
        }
        
        if provider_type == "reinhard_transfer" and "reference_path" in params:
            ref_path = params["reference_path"]
            ref_media = probe_media(ref_path)
            ref_frames = sample_frames(ref_path, num_samples=1, duration=ref_media.duration)
            context["reference_frame"] = ref_frames[0]

        suggestion = provider.analyze_and_suggest(analysis, context=context)
        recipe = self._complete(suggestion.recipe)
        suggestion.recipe = recipe
        
        directory = job_directory(job_id)
        write_json(directory / "media_info.json", media.model_dump(mode="json"))
        write_json(directory / "analysis.json", analysis.model_dump(mode="json"))
        write_json(directory / "grade_recipe.json", recipe.model_dump(mode="json"))
        
        analysis_dict = analysis.model_dump(mode="json")
        suggestion_dict = suggestion.model_dump(mode="json")
        
        return {
            "media_info": media.model_dump(mode="json"),
            "analysis": analysis_dict,
            "analysis_report": analysis_dict,
            "auto_advice": suggestion_dict,
            "recipe": recipe.model_dump(mode="json")
        }

    def _handle_transfer(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        source_path = params["source_path"]
        reference_path = params["reference_path"]
        
        logger.info(f"Running Reinhard color transfer: source={source_path}, ref={reference_path}")
        source_media = probe_media(source_path)
        source_frames = sample_frames(source_path, num_samples=3, duration=source_media.duration)
        
        ref_media = probe_media(reference_path)
        ref_frames = sample_frames(reference_path, num_samples=3, duration=ref_media.duration)
        
        analysis = analyze_image_frames(source_frames)
        provider = suggestion_registry.get("reinhard_transfer")
        suggestion = provider.analyze_and_suggest(
            analysis,
            context={"source_frame": source_frames[0], "reference_frame": ref_frames[0]}
        )
        recipe = self._complete(suggestion.recipe)
        
        directory = job_directory(job_id)
        write_json(directory / "grade_recipe.json", recipe.model_dump(mode="json"))
        lut_path = bake_3d_lut(recipe, str(directory / "output.cube"), self.ocio)
        
        return {
            "recipe": recipe.model_dump(mode="json"),
            "lut_path": lut_path,
            "suggestion": suggestion.model_dump(mode="json")
        }

    def _handle_recipe(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        analysis_job_id = params.pop("analysis_job_id", None)
        base: dict[str, Any] = {}
        if analysis_job_id:
            source = db_manager.get_job(analysis_job_id)
            if not source or source["status"] != "completed":
                raise ValueError(f"Analysis job is not completed: {analysis_job_id}")
            base = dict((source.get("result_data") or {}).get("recipe") or {})
        allowed = set(GradeRecipe.model_fields)
        base.update({k: v for k, v in params.items() if k in allowed and v is not None})
        recipe = self._complete(GradeRecipe.model_validate(base))
        write_json(job_directory(job_id) / "grade_recipe.json", recipe.model_dump(mode="json"))
        return {"recipe": recipe.model_dump(mode="json")}

    def _handle_lut(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        recipe = self._recipe_from_params(params)
        directory = job_directory(job_id)
        write_json(directory / "grade_recipe.json", recipe.model_dump(mode="json"))
        path = bake_3d_lut(recipe, str(directory / "output.cube"), self.ocio)
        return {"recipe": recipe.model_dump(mode="json"), "lut_path": path, "lut_size": recipe.lut_size}

    def _handle_preview(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        recipe = self._recipe_from_params(params)
        source, directory = params["input_path"], job_directory(job_id)
        media = probe_media(source)
        write_json(directory / "media_info.json", media.model_dump(mode="json"))
        write_json(directory / "grade_recipe.json", recipe.model_dump(mode="json"))
        lut = bake_3d_lut(recipe, str(directory / "output.cube"), self.ocio)
        output = render_preview(source, lut, str(directory / "preview.mp4"), params.get("target_height", 720), params.get("split_screen", False))
        qc = perform_quality_control(source, output)
        write_json(directory / "quality_report.json", qc.model_dump(mode="json"))
        return {"recipe": recipe.model_dump(mode="json"), "preview_path": output, "quality_report": qc.model_dump(mode="json")}

    def _handle_render(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        recipe = self._recipe_from_params(params)
        source, directory = params["input_path"], job_directory(job_id)
        media = probe_media(source)
        write_json(directory / "media_info.json", media.model_dump(mode="json"))
        write_json(directory / "grade_recipe.json", recipe.model_dump(mode="json"))
        lut = bake_3d_lut(recipe, str(directory / "output.cube"), self.ocio)
        output = render_final(source, lut, str(directory / "graded_video.mp4"))
        qc = perform_quality_control(source, output)
        write_json(directory / "quality_report.json", qc.model_dump(mode="json"))
        if not qc.passed:
            raise RuntimeError("Rendered output failed quality control: " + "; ".join(qc.errors))
        return {"recipe": recipe.model_dump(mode="json"), "output_path": output, "quality_report": qc.model_dump(mode="json")}

    def _handle_scenes(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        result = detect_and_analyze_scenes(
            params["file_path"], detector=params.get("detector", "adaptive"),
            threshold=float(params.get("threshold", 3.0)),
            min_scene_len=int(params.get("min_scene_len", 12)), analyze=bool(params.get("analyze", True)),
            artifact_dir=str(job_directory(job_id)),
        )
        write_json(job_directory(job_id) / "scene_analysis.json", result.model_dump(mode="json"))
        return {"scene_analysis": result.model_dump(mode="json"), "scene_count": len(result.scenes)}

    def _handle_project_recipe(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        scene_job_id = params["scene_job_id"]
        source = self._completed_job(scene_job_id, "Scene analysis")
        scene_data = (source.get("result_data") or {}).get("scene_analysis")
        if not scene_data:
            raise ValueError(f"Scene job has no analysis result: {scene_job_id}")
        analysis = SceneAnalysisResult.model_validate(scene_data)
        project = project_from_scene_analysis(
            analysis.media_info.file_path, analysis, params.get("look_id", "neutral_broadcast"), scene_job_id,
        )
        project.source_media_hash = compute_file_sha256(analysis.media_info.file_path)
        project.source_artifact_id = f"source_{project.source_media_hash[:16]}" if project.source_media_hash else ""
        project.workflow = params.get("workflow", "professional_assisted")
        quality_profile = params.get("quality_profile", "balanced")
        if quality_profile not in {"broadcast_safe", "balanced", "creative"}:
            raise ValueError(f"Unknown quality profile: {quality_profile}")
        project.quality_profile = quality_profile
        if bool(params.get("auto_group", True)):
            project.scene_groups = suggest_scene_groups(
                analysis,
                similarity_threshold=float(params.get("grouping_threshold", 0.48)),
                # Performance guard only since V0.6; it no longer drives grouping.
                max_group_shots=int(params.get("max_group_shots", 24)),
                # How far apart in time two shots may still belong to one
                # lighting setup. Widen it for long scenes shot in one location.
                time_window=float(params.get("grouping_time_window", 45.0)),
            )
        # Harmonise white balance inside each scene group before anything else
        # consumes base_correction. Shots whose own estimate was unusable adopt
        # the group's consensus instead of staying uncorrected next to corrected
        # neighbours, which is where most intra-group white-balance jumps came
        # from.
        white_balance_diagnostics: list[dict[str, Any]] = []
        if bool(params.get("harmonize_white_balance", True)):
            white_balance_diagnostics = harmonize_group_white_balance(project, analysis)
        shot_match_diagnostics: list[dict[str, Any]] = []
        if project.workflow == "automatic":
            shot_match_diagnostics = self._apply_shot_match(project, analysis)
        # Resolve OCIO metadata and hashes before persisting the editable project.
        for shot in project.shots:
            shot.base_correction = self._complete(shot.base_correction)
        write_json(job_directory(job_id) / "project_recipe.json", project.model_dump(mode="json"))
        return {
            "project_recipe": project.model_dump(mode="json"), "shot_count": len(project.shots),
            "scene_group_count": len(project.scene_groups), "revision": project.revision,
            "shot_match_diagnostics": shot_match_diagnostics,
            "white_balance_diagnostics": white_balance_diagnostics,
        }

    def _handle_grade_plan_revise(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        project = self._project_from_job(params["project_job_id"])
        expected = int(params["expected_revision"])
        if project.revision != expected:
            raise ValueError(f"GradePlan revision conflict: expected {expected}, current {project.revision}")
        if params.get("project_look"):
            project.project_look = params["project_look"]
        if params.get("scene_groups") is not None:
            project.scene_groups = [SceneGroup.model_validate(item) for item in params["scene_groups"]]
        updates = {item["shot_id"]: item for item in params.get("shot_updates", [])}
        for shot in project.shots:
            update = updates.get(shot.shot_id or shot.scene_id)
            if not update:
                continue
            recipe_data = shot.base_correction.model_dump()
            for key in (
                "exposure", "temperature", "tint", "contrast", "pivot", "saturation",
                "highlight_rolloff", "highlight_softness", "color_density",
                "skin_protection", "gamut_protection", "rgb_gains", "lift", "gamma", "gain",
            ):
                if update.get(key) is not None:
                    recipe_data[key] = update[key]
            shot.base_correction = self._complete(GradeRecipe.model_validate(recipe_data))
            if update.get("look_strength") is not None:
                shot.look_strength = float(update["look_strength"])
            if update.get("enabled") is not None:
                shot.enabled = bool(update["enabled"])
        # Revalidate group coverage after mutations, then record the new revision.
        project = ProjectGradeRecipe.model_validate(project.model_dump())
        project.revise(
            "manual_revision", params.get("actor", "local-user"),
            {"updated_shots": sorted(updates), "scene_groups_replaced": params.get("scene_groups") is not None},
        )
        write_json(job_directory(job_id) / "project_recipe.json", project.model_dump(mode="json"))
        return {"project_recipe": project.model_dump(mode="json"), "revision": project.revision, "status": project.status}

    def _handle_grade_plan_approve(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        project = self._project_from_job(params["project_job_id"])
        expected = int(params["expected_revision"])
        if project.revision != expected:
            raise ValueError(f"GradePlan revision conflict: expected {expected}, current {project.revision}")
        project.approve(params.get("actor", "local-user"))
        bundle = job_directory(job_id) / f"approved_revision_{project.revision:04d}"
        manifest = freeze_approved_revision(project, bundle)
        append_event(job_id, "approval.assets_frozen", {
            "revision": project.revision, "asset_count": len(manifest.get("assets", [])),
            "manifest_sha256": manifest.get("manifest_sha256"),
        })
        write_json(job_directory(job_id) / "project_recipe.json", project.model_dump(mode="json"))
        return {
            "project_recipe": project.model_dump(mode="json"), "revision": project.revision,
            "status": project.status, "approved_by": project.approved_by, "approved_at": project.approved_at,
            "approval_bundle_path": str(bundle), "approval_manifest": manifest,
        }

    @staticmethod
    def _shot_lut_role(project: ProjectGradeRecipe, shot) -> str:
        """Classify a baked shot LUT so QC can apply the right tolerances.

        A shot whose effective creative strength is zero produces a purely
        technical LUT. Judging those by creative-LUT rules is what generated the
        historical wave of non-actionable warnings, so the distinction is made
        here, at the only place that actually knows.
        """
        if not shot.enabled:
            return "technical"
        group = project.group_for_shot(shot.shot_id or shot.scene_id)
        if group and group.creative_transform:
            strength = shot.look_strength * group.creative_transform.strength
            return "combined" if strength > 0.0 else "technical"
        return "combined" if shot.look_strength > 0.0 else "technical"

    def _bake_safe_shot_lut(
        self, project: ProjectGradeRecipe, shot, lut_dir: Path,
    ) -> tuple[Any, str, dict[str, Any] | None]:
        """Bake one shot's transform stack and return it plus any fallback.

        When a creative transform is present the result is a *list* of LUTs to
        be chained at render time, not a single composed LUT: composition has to
        clip the technical layer's output to [0,1] on an 8-bit grid before the
        creative layer sees it, which throws away recovered highlights.
        """
        role = self._shot_lut_role(project, shot)
        group = project.group_for_shot(shot.shot_id or shot.scene_id)
        external = project.resolve_transform_path(group.creative_transform) if group and group.creative_transform else ""
        if external:
            source_lut = Path(external).resolve()
            if not source_lut.is_file():
                raise ValueError(f"Creative transform is missing: {source_lut}")
            technical_path = bake_3d_lut(
                self._complete(project.technical_recipe(shot)),
                str(lut_dir / f"{shot.scene_id}_technical.cube"), self.ocio,
            )
            strength = shot.look_strength * group.creative_transform.strength
            creative_path = scale_cube_strength(
                str(source_lut), strength, str(lut_dir / f"{shot.scene_id}_creative.cube"),
            )
            path = [technical_path, creative_path]
        else:
            effective = GradeRecipe() if not shot.enabled else project.effective_recipe(shot)
            path = bake_3d_lut(
                self._complete(effective), str(lut_dir / f"{shot.scene_id}.cube"), self.ocio,
            )
        # Each layer is inspected in its own right; a chained stack has no single
        # composed file to measure, and measuring the layers is more informative.
        layer_roles = ["technical", "creative"] if isinstance(path, list) else [role]
        layer_paths = path if isinstance(path, list) else [path]
        reports = [
            inspect_cube_lut(item, layer_role)
            for item, layer_role in zip(layer_paths, layer_roles)
        ]
        original_report = next((item for item in reports if not item.passed), reports[0])
        if all(item.passed for item in reports):
            return path, role, None
        technical_recipe = GradeRecipe() if not shot.enabled else project.technical_recipe(shot)
        fallback_path = bake_3d_lut(
            self._complete(technical_recipe),
            str(lut_dir / f"{shot.scene_id}_safe_technical.cube"), self.ocio,
        )
        fallback_report = inspect_cube_lut(fallback_path, "technical")
        fallback_kind = "technical_only"
        if not fallback_report.passed:
            fallback_path = bake_3d_lut(
                self._complete(GradeRecipe()),
                str(lut_dir / f"{shot.scene_id}_safe_identity.cube"), self.ocio,
            )
            fallback_report = inspect_cube_lut(fallback_path, "technical")
            fallback_kind = "identity"
        if not fallback_report.passed:
            raise RuntimeError(f"Unable to create a safe LUT for {shot.scene_id}")
        return fallback_path, "technical", {
            "scene_id": shot.scene_id, "replaced_lut": path, "fallback_lut": fallback_path,
            "fallback_kind": fallback_kind, "reason": list(original_report.warnings),
        }

    def _handle_project_render(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        project_job_id = params["project_job_id"]
        project = self._project_from_job(project_job_id, "Project recipe")
        input_path = params.get("input_path") or project.source_path
        directory = job_directory(job_id)
        lut_dir = directory / "shot_luts"
        lut_dir.mkdir(parents=True, exist_ok=True)
        preview = bool(params.get("preview", False))
        if not preview and project.status != "approved" and not bool(params.get("allow_unapproved", False)):
            raise ValueError("Formal render requires an approved GradePlan")
        if not preview and project.approval_bundle_path:
            verification = verify_approval_bundle(project.approval_bundle_path)
            if not verification["valid"]:
                raise ValueError("Approved asset bundle failed verification: " + "; ".join(verification["errors"]))
            input_path = project.source_path
        elif not Path(input_path).is_file():
            # Distinguish "the file is gone" from "the file changed". They need
            # completely different responses, and the hash check reported both
            # as a mismatch - which sent an investigation looking for a
            # corrupted file when the uploads directory had simply been cleared.
            raise FileNotFoundError(
                f"Render input no longer exists: {input_path}. The GradePlan was built "
                f"from this file; re-upload the source, or start a new project from the "
                f"current media."
            )
        elif project.source_media_hash and compute_file_sha256(input_path) != project.source_media_hash:
            raise ValueError(
                f"Render input does not match the GradePlan: {input_path} exists but its "
                f"content hash differs from the one recorded when the plan was approved. "
                f"The file was replaced or re-encoded since."
            )
        # Decide the stable output target before doing any expensive work.  A
        # rendered checkpoint can then be verified and resumed after a worker
        # process is interrupted between encoding and final QC.
        profile = str(params.get("output_profile") or ("preview" if preview else "delivery"))
        if profile not in ("preview", "delivery", "master"):
            raise ValueError(f"Unknown output profile: {profile}")
        if preview:
            output_name = "project_preview.mp4"
        elif profile == "master":
            _, master_codec = video_encoder_args(profile, get_ffmpeg_executable())
            output_name = f"project_graded_master{container_suffix(profile, master_codec)}"
        else:
            output_name = "project_graded_video.mp4"
        output_target = directory / output_name
        checkpoint_path = directory / "render_checkpoint.json"
        shot_specs: list[tuple[float, float, str]] = []
        face_specs: list[tuple[float, float, str, str]] = []
        lut_paths: list[str] = []
        lut_roles: list[tuple[str, str]] = []
        safety_fallbacks: list[dict[str, Any]] = []
        scene_job = self._completed_job(project.scene_analysis_job_id, "Scene analysis")
        scene_analysis = SceneAnalysisResult.model_validate(scene_job["result_data"]["scene_analysis"])
        scenes_by_id = {scene.scene_id: scene for scene in scene_analysis.scenes}
        face_selective = os.getenv("FACE_SELECTIVE_RENDER", "1") != "0" and any(
            scene.face_analysis and scene.face_analysis.face_count > 0 for scene in scene_analysis.scenes
        )
        face_strength = max(0.0, min(1.0, float(os.getenv("FACE_CREATIVE_STRENGTH", "0.30"))))

        def finish_render(
            output: str, completed_luts: list[str], completed_roles: list[tuple[str, str]],
            fallbacks: list[dict[str, Any]], face_applied: bool,
        ) -> dict[str, Any]:
            render_qc = perform_quality_control(input_path, output)
            project_qc = evaluate_project_quality(project, scene_analysis, completed_roles, rendered_path=output)
            db_manager.update_job_progress(job_id, QC_END, self.worker_id)
            append_event(job_id, "render.qc_completed", {
                "render_passed": render_qc.passed, "final_decision": project_qc.final_decision,
                "base_correction": project_qc.base_correction, "creative_transform": project_qc.creative_transform,
                "within_group_continuity": project_qc.within_group_continuity,
                "review_item_count": len(project_qc.review_items), "output_path": output,
            })
            if fallbacks:
                project_qc.warnings.append(f"{len(fallbacks)} unstable creative LUT(s) were replaced with safe fallback LUTs")
            write_json(directory / "project_recipe.json", project.model_dump(mode="json"))
            write_json(directory / "quality_report.json", render_qc.model_dump(mode="json"))
            write_json(directory / "project_quality_report.json", project_qc.model_dump(mode="json"))
            if not render_qc.passed or project_qc.final_decision == "FAIL":
                raise RuntimeError("Project render failed quality control")
            return {
                "project_recipe": project.model_dump(mode="json"), "output_path": output,
                "shot_luts": completed_luts, "quality_report": render_qc.model_dump(mode="json"),
                "project_quality_report": project_qc.model_dump(mode="json"), "safety_fallbacks": fallbacks,
                "face_selective_render": face_applied, "face_creative_strength": face_strength,
            }
        # Stage weights for progress. Baking is proportional to shot count and
        # the render dominates everything else, so the render gets the widest
        # band and reports inside itself from FFmpeg.
        BAKE_START, BAKE_END, RENDER_END, QC_END = 8, 35, 92, 98
        # Only trust a checkpoint tied to this exact immutable project revision
        # and source path. Atomic output replacement means its media is either
        # complete or absent; QC below remains the final gate.
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            checkpoint = None
        if (
            checkpoint and checkpoint.get("stage") == "rendered"
            and checkpoint.get("project_job_id") == project_job_id
            and checkpoint.get("project_revision") == project.revision
            and checkpoint.get("source_path") == str(Path(input_path).resolve())
            and checkpoint.get("output_path") == str(output_target.resolve())
            and output_target.is_file() and output_target.stat().st_size > 0
        ):
            db_manager.update_job_progress(job_id, RENDER_END, self.worker_id)
            append_event(job_id, "render.resumed_from_checkpoint", {"output_path": str(output_target)})
            return finish_render(
                str(output_target), list(checkpoint.get("shot_luts") or []),
                [tuple(item) for item in checkpoint.get("lut_roles") or []],
                list(checkpoint.get("safety_fallbacks") or []), bool(checkpoint.get("face_selective_render")),
            )
        total_shots = max(1, len(project.shots))
        for index, shot in enumerate(project.shots):
            # Baking a LUT per shot is minutes of in-process work on a long
            # project, so it needs its own cancellation checkpoint.
            raise_if_cancelled()
            if index % 5 == 0:
                db_manager.update_job_progress(
                    job_id, BAKE_START + int((BAKE_END - BAKE_START) * index / total_shots), self.worker_id
                )
            path, role, fallback = self._bake_safe_shot_lut(project, shot, lut_dir)
            if fallback:
                safety_fallbacks.append(fallback)
            layers = path if isinstance(path, list) else [path]
            lut_paths.extend(layers)
            # QC sees each layer under its own role, so a technical correction is
            # never judged by creative-LUT tolerances.
            if len(layers) > 1:
                lut_roles.extend([(layers[0], "technical"), (layers[1], "creative")])
            else:
                lut_roles.append((layers[0], role))
            shot_specs.append((shot.start_time, shot.end_time, path))
            face_path = path
            scene = scenes_by_id.get(shot.scene_id)
            group = project.group_for_shot(shot.shot_id or shot.scene_id)
            if (
                face_selective and not fallback and scene and scene.face_analysis and scene.face_analysis.face_count
                and group and group.creative_transform and group.creative_transform.asset_path
            ):
                technical_path = bake_3d_lut(
                    self._complete(project.technical_recipe(shot)),
                    str(lut_dir / f"{shot.scene_id}_face_technical.cube"), self.ocio,
                )
                effective_recipe = project.effective_recipe(shot)
                skin_protection = max(0.0, min(1.0, float(effective_recipe.skin_protection)))
                protected_strength = face_strength * (1.0 - 0.70 * skin_protection)
                protected_creative = scale_cube_strength(
                    project.resolve_transform_path(group.creative_transform),
                    shot.look_strength * group.creative_transform.strength * protected_strength,
                    str(lut_dir / f"{shot.scene_id}_face_creative.cube"),
                )
                if inspect_cube_lut(protected_creative, "creative").passed:
                    face_path = [technical_path, protected_creative]
                else:
                    face_path = technical_path
            face_specs.append((shot.start_time, shot.end_time, path, face_path))
        append_event(job_id, "render.luts_ready", {
            "revision": project.revision, "shot_count": len(project.shots),
            "scene_group_count": len(project.scene_groups), "fallback_count": len(safety_fallbacks),
        })
        db_manager.update_job_progress(job_id, BAKE_END, self.worker_id)

        def _render_progress(fraction: float) -> None:
            db_manager.update_job_progress(
                job_id, BAKE_END + int((RENDER_END - BAKE_END) * max(0.0, min(1.0, fraction))), self.worker_id
            )

        face_selective_applied = face_selective and any(full != protected for _, _, full, protected in face_specs)
        if face_selective_applied:
            try:
                output = render_face_selective_timeline(
                    input_path, face_specs, str(directory / output_name),
                    target_height=int(params.get("target_height", 0)),
                    on_progress=_render_progress,
                )
            except JobCancelled:
                raise
            except Exception as exc:
                record = report_degradation(logger, "Face-selective render", exc)
                face_selective_applied = False
                safety_fallbacks.append({
                    "fallback_kind": "normal_timeline",
                    "reason": [str(exc)],
                    **{key: record[key] for key in ("error_type", "cause", "severity")},
                })
                append_event(job_id, "render.degraded", record)
                output = render_shot_timeline(
                    input_path, shot_specs, str(directory / output_name),
                    target_height=int(params.get("target_height", 0)), preview=preview,
                    profile=profile, on_progress=_render_progress,
                )
        else:
            output = render_shot_timeline(
                input_path, shot_specs, str(directory / output_name),
                target_height=int(params.get("target_height", 0)), preview=preview,
                profile=profile, on_progress=_render_progress,
            )
        db_manager.update_job_progress(job_id, RENDER_END, self.worker_id)
        write_json(checkpoint_path, {
            "stage": "rendered", "project_job_id": project_job_id, "project_revision": project.revision,
            "source_path": str(Path(input_path).resolve()), "output_path": str(Path(output).resolve()),
            "shot_luts": lut_paths, "lut_roles": lut_roles, "safety_fallbacks": safety_fallbacks,
            "face_selective_render": face_selective_applied,
        })
        return finish_render(output, lut_paths, lut_roles, safety_fallbacks, face_selective_applied)

    def _handle_timeline_preview(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Render only a selected shot (with context) or a SceneGroup montage."""
        project_job_id = params["project_job_id"]
        project = self._project_from_job(project_job_id)
        scope = params.get("scope", "shot")
        selected: list[tuple[Any, float, float]] = []
        if scope == "shot":
            shot_id = params.get("shot_id")
            shot = next((item for item in project.shots if (item.shot_id or item.scene_id) == shot_id), None)
            if shot is None:
                raise ValueError(f"Unknown shot: {shot_id}")
            context = float(params.get("context_seconds", 1.0))
            window_start = max(0.0, shot.start_time - context)
            window_end = min(probe_media(project.source_path).duration, shot.end_time + context)
            for item in project.shots:
                start, end = max(window_start, item.start_time), min(window_end, item.end_time)
                if end > start:
                    selected.append((item, start, end))
            target_id = shot_id
        else:
            group_id = params.get("scene_group_id")
            group = next((item for item in project.scene_groups if item.scene_group_id == group_id), None)
            if group is None:
                raise ValueError(f"Unknown scene group: {group_id}")
            ids = set(group.shot_ids)
            selected = [(item, item.start_time, item.end_time) for item in project.shots if (item.shot_id or item.scene_id) in ids]
            target_id = group_id
        if not selected:
            raise ValueError("Timeline preview selection is empty")

        directory = job_directory(job_id)
        lut_dir = directory / "shot_luts"
        lut_dir.mkdir(parents=True, exist_ok=True)
        specs: list[tuple[float, float, str]] = []
        fallbacks: list[dict[str, Any]] = []
        for shot, start, end in selected:
            lut_path, fallback = self._bake_safe_shot_lut(project, shot, lut_dir)
            specs.append((start, end, lut_path))
            if fallback:
                fallbacks.append(fallback)

        height = int(params.get("target_height", 540))
        cache_key = hashlib.sha256(
            f"timeline-v2|{project_job_id}|{project.revision}|{scope}|{target_id}|{params.get('context_seconds', 1.0)}|{height}".encode("utf-8")
        ).hexdigest()[:24]
        cache_dir = Path(__file__).resolve().parent.parent / "data" / "cache" / "timeline"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_output = cache_dir / f"{cache_key}.mp4"
        cache_hit = cached_output.is_file() and cached_output.stat().st_size > 0
        if not cache_hit:
            render_shot_timeline(
                project.source_path, specs, str(cached_output), target_height=height,
                preview=True, trim_audio=True,
            )
        output = directory / "timeline_preview.mp4"
        shutil.copy2(cached_output, output)
        info = probe_media(str(output))
        return {
            "project_recipe": project.model_dump(mode="json"), "output_path": str(output),
            "scope": scope, "target_id": target_id, "segments": [
                {"shot_id": shot.shot_id or shot.scene_id, "start_time": start, "end_time": end}
                for shot, start, end in selected
            ],
            "duration": info.duration, "cached": cache_hit,
            "render_backend": "NVENC preview + CPU tetrahedral LUT", "safety_fallbacks": fallbacks,
        }

    @staticmethod
    def _apply_bounded_qc_repair(project: ProjectGradeRecipe, targets: list[Any], category: str) -> None:
        """Apply one deterministic, bounded repair to a set of shots."""
        for shot in targets:
            data = shot.base_correction.model_dump()
            touched: set[str]
            if category == "large_exposure_correction":
                data["exposure"] = max(-2.0, min(2.0, float(data.get("exposure", 0.0)) * 0.65))
                data["highlight_rolloff"] = max(float(data.get("highlight_rolloff", 0.0)), 0.30)
                data["highlight_softness"] = max(float(data.get("highlight_softness", 0.0)), 0.30)
                touched = {"exposure", "highlight_rolloff", "highlight_softness"}
            elif category == "highlight_clipping":
                data["exposure"] = max(-2.0, min(2.0, float(data.get("exposure", 0.0)) - 0.15))
                data["highlight_rolloff"] = max(float(data.get("highlight_rolloff", 0.0)), 0.40)
                data["highlight_softness"] = max(float(data.get("highlight_softness", 0.0)), 0.40)
                touched = {"exposure", "highlight_rolloff", "highlight_softness"}
            elif category == "skin_safety":
                shot.look_strength = max(0.0, min(1.0, float(shot.look_strength) * 0.75))
                data["skin_protection"] = max(float(data.get("skin_protection", 0.0)), 0.95)
                touched = {"skin_protection"}
            else:
                raise ValueError(f"Unsupported automatic repair category: {category}")
            sources = dict(data.get("parameter_sources") or {})
            confidence = dict(data.get("parameter_confidence") or {})
            for key in touched:
                sources[key] = "qc_auto_repair"
                confidence[key] = max(float(confidence.get(key, 0.0)), 0.88)
            data["parameter_sources"] = sources
            data["parameter_confidence"] = confidence
            data["rationales"] = list(data.get("rationales") or []) + [
                f"Applied bounded QC repair for {category}."
            ]
            shot.base_correction = GradeRecipe.model_validate(data)

    def _handle_qc_auto_repair(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Apply many QC repair items atomically as one new GradePlan revision."""
        project = self._project_from_job(params["project_job_id"])
        expected = int(params["expected_revision"])
        if project.revision != expected:
            raise ValueError(f"GradePlan revision conflict: expected {expected}, current {project.revision}")
        repairs = params.get("repairs") or []
        shots_by_id = {shot.shot_id or shot.scene_id: shot for shot in project.shots}
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in repairs:
            shot_id, category = str(item.get("shot_id", "")), str(item.get("category", ""))
            if not shot_id or shot_id not in shots_by_id:
                raise ValueError(f"Unknown repair shot: {shot_id}")
            if category not in {
                "large_exposure_correction", "highlight_clipping", "skin_safety",
                "continuity_luminance", "continuity_white_balance",
            }:
                raise ValueError(f"Unsupported automatic repair category: {category}")
            key = (shot_id, category)
            if key not in seen:
                normalized.append({"shot_id": shot_id, "category": category})
                seen.add(key)
        if not normalized:
            raise ValueError("Batch QC repair selection is empty")
        continuity = [item for item in normalized if item["category"] in {"continuity_luminance", "continuity_white_balance"}]
        match_diagnostics: list[dict[str, Any]] = []
        if continuity:
            scene_job = self._completed_job(project.scene_analysis_job_id, "Scene analysis")
            analysis = SceneAnalysisResult.model_validate(scene_job["result_data"]["scene_analysis"])
            match_diagnostics = self._apply_shot_match(project, analysis, {item["shot_id"] for item in continuity})
        for category in ("large_exposure_correction", "highlight_clipping", "skin_safety"):
            targets = [shots_by_id[item["shot_id"]] for item in normalized if item["category"] == category]
            if targets:
                self._apply_bounded_qc_repair(project, targets, category)
                for shot in targets:
                    shot.base_correction = self._complete(shot.base_correction)
        updated_shots = sorted({item["shot_id"] for item in normalized})
        project.revise(
            "qc_batch_auto_repair", params.get("actor", "task-qc-batch-repair"),
            {"repair_count": len(normalized), "updated_shots": updated_shots, "categories": sorted({item["category"] for item in normalized})},
        )
        write_json(job_directory(job_id) / "project_recipe.json", project.model_dump(mode="json"))
        return {
            "project_recipe": project.model_dump(mode="json"), "revision": project.revision,
            "updated_shots": updated_shots, "repair_count": len(normalized),
            "repairs": normalized, "shot_match_diagnostics": match_diagnostics,
        }

    def _handle_timeline_adjust(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        project = self._project_from_job(params["project_job_id"])
        expected = int(params["expected_revision"])
        if project.revision != expected:
            raise ValueError(f"GradePlan revision conflict: expected {expected}, current {project.revision}")
        scope = params.get("scope", "shot")
        if scope == "shot":
            target_ids = {params.get("shot_id")}
        else:
            group = next((item for item in project.scene_groups if item.scene_group_id == params.get("scene_group_id")), None)
            if group is None:
                raise ValueError(f"Unknown scene group: {params.get('scene_group_id')}")
            target_ids = set(group.shot_ids)
        targets = [item for item in project.shots if (item.shot_id or item.scene_id) in target_ids]
        if not targets:
            raise ValueError("Timeline adjustment selection is empty")
        operation = params["operation"]
        if operation == "restore_auto":
            scene_job = self._completed_job(project.scene_analysis_job_id, "Scene analysis")
            analysis = SceneAnalysisResult.model_validate(scene_job["result_data"]["scene_analysis"])
            suggestions = {item.scene_id: item.suggested_recipe for item in analysis.scenes}
            for shot in targets:
                suggested = suggestions.get(shot.scene_id)
                if suggested is not None:
                    shot.base_correction = self._complete(suggested)
                    shot.shot_match = None
        elif operation == "match_hero":
            scene_job = self._completed_job(project.scene_analysis_job_id, "Scene analysis")
            analysis = SceneAnalysisResult.model_validate(scene_job["result_data"]["scene_analysis"])
            match_diagnostics = self._apply_shot_match(project, analysis, target_ids)
        elif operation == "auto_repair":
            category = params.get("repair_category")
            if category in {"continuity_luminance", "continuity_white_balance"}:
                scene_job = self._completed_job(project.scene_analysis_job_id, "Scene analysis")
                analysis = SceneAnalysisResult.model_validate(scene_job["result_data"]["scene_analysis"])
                match_diagnostics = self._apply_shot_match(project, analysis, target_ids)
            else:
                self._apply_bounded_qc_repair(project, targets, category)
                for shot in targets:
                    shot.base_correction = self._complete(shot.base_correction)
        project.revise(
            operation, params.get("actor", "local-user"),
            {"scope": scope, "shot_ids": sorted(target_ids), "repair_category": params.get("repair_category", "")},
        )
        write_json(job_directory(job_id) / "project_recipe.json", project.model_dump(mode="json"))
        return {
            "project_recipe": project.model_dump(mode="json"), "revision": project.revision,
            "updated_shots": sorted(target_ids),
            "shot_match_diagnostics": match_diagnostics if operation == "match_hero" or (
                operation == "auto_repair" and params.get("repair_category") in {"continuity_luminance", "continuity_white_balance"}
            ) else [],
        }

    def _handle_reference_preflight(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        from color_core.frame_sampler import sample_frames_at

        reference_path = params["reference_path"]
        reference_media = probe_media(reference_path)
        reference_time = reference_media.duration / 2.0 if reference_media.is_video else 0.0
        reference_frame = sample_frames_at(reference_path, [reference_time], target_height=720)[0]
        report = inspect_reference_frame(reference_frame)
        analysis = analyze_image_frames([reference_frame])
        preview_path = job_directory(job_id) / "reference_preview.jpg"
        cv2.imwrite(str(preview_path), reference_frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        suitability = 1.0
        compatibility_warnings: list[str] = []
        project_job_id = params.get("project_job_id")
        compared_groups: list[str] = []
        if project_job_id:
            project = self._project_from_job(project_job_id)
            requested = params.get("scene_group_id")
            groups = project.scene_groups if not requested or requested == "__all__" else [
                item for item in project.scene_groups if item.scene_group_id == requested
            ]
            shots = {item.shot_id or item.scene_id: item for item in project.shots}
            heroes = [shots[item.hero_shot_id] for item in groups]
            source_frames = sample_frames_at(
                project.source_path,
                [(item.start_time + item.end_time) / 2.0 for item in heroes], target_height=720,
            )
            source_analysis = analyze_image_frames(source_frames)
            lum_delta = abs(source_analysis.luminance.median - analysis.luminance.median)
            sat_delta = abs(source_analysis.saturation.mean - analysis.saturation.mean)
            suitability = max(0.0, 1.0 - lum_delta * 0.9 - sat_delta * 0.45)
            if lum_delta > 0.35:
                compatibility_warnings.append("参考图与目标场景亮度结构差异较大，建议按 SceneGroup 分别提供参考图")
            if sat_delta > 0.45:
                compatibility_warnings.append("参考图与目标场景色彩浓度差异较大，CanonCGT 结果可能需要降低强度")
            compared_groups = [item.scene_group_id for item in groups]
        recommendations = [
            "优先使用无字幕、无黑边、无播放器界面的 sRGB/Rec.709 成片画面",
            "参考图应与目标场景主体、光线方向和明暗结构尽量接近",
        ]
        if min(report["width"], report["height"]) < 512:
            recommendations.append("建议换用短边至少 512 像素、最好 1080p 的参考图")
        return {
            "reference_report": report, "analysis": analysis.model_dump(mode="json"),
            "preview_path": str(preview_path), "preview_url": f"/v1/jobs/{job_id}/artifacts/reference_preview.jpg",
            "suitability_score": round(suitability, 3), "compatibility_warnings": compatibility_warnings,
            "recommendations": recommendations, "compared_scene_group_ids": compared_groups,
        }

    def _handle_export(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        source_job = self._completed_job(params["source_job_id"], "Export source")
        result = source_job.get("result_data") or {}
        project_data = result.get("project_recipe")
        project = ProjectGradeRecipe.model_validate(project_data) if project_data else None
        recipe_data = result.get("recipe")
        if project:
            shot = next((item for item in project.shots if item.scene_id == params.get("scene_id")), project.shots[0])
            recipe = self._complete(project.effective_recipe(shot))
        elif recipe_data:
            recipe = self._complete(GradeRecipe.model_validate(recipe_data))
        else:
            raise ValueError("Export source contains neither a project recipe nor a grade recipe")
        directory = job_directory(job_id)
        fmt = params["format"]
        if fmt == "cc":
            path = export_cc(recipe, str(directory / "grade.cc"), params.get("scene_id") or "grade")
        elif fmt == "ccc":
            if project is None:
                raise ValueError("CCC export requires a project recipe")
            path = export_ccc(project, str(directory / "project.ccc"))
        elif fmt == "clf":
            path = export_clf(recipe, str(directory / "grade.clf"), self.ocio)
        elif fmt == "cube":
            path = bake_3d_lut(recipe, str(directory / "grade.cube"), self.ocio)
        else:  # protected by Pydantic, retained for worker recovery safety
            raise ValueError(f"Unsupported export format: {fmt}")
        return {"format": fmt, "export_path": path, "recipe": recipe.model_dump(mode="json")}

    def _handle_adaint_lut(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        source = params["input_path"]
        media = probe_media(source)
        timestamp = params.get("sample_time")
        if timestamp is None:
            timestamp = media.duration / 2.0 if media.is_video else 0.0
        from color_core.frame_sampler import sample_frames_at
        frame = sample_frames_at(source, [float(timestamp)])[0]
        result = generate_adaint_lut(
            frame, str(job_directory(job_id) / "adaint.cube"), params.get("checkpoint_path"),
            int(params.get("lut_size", 33)),
        )
        return {**result, "sample_time": timestamp}

    def _handle_scene_group_adaint(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Generate one AdaInt creative LUT per selected SceneGroup and revise the GradePlan."""
        from color_core.frame_sampler import sample_frames_at

        project = self._project_from_job(params["project_job_id"])
        expected = int(params["expected_revision"])
        if project.revision != expected:
            raise ValueError(f"GradePlan revision conflict: expected {expected}, current {project.revision}")
        requested = params.get("scene_group_ids")
        target_ids = set(requested or [group.scene_group_id for group in project.scene_groups])
        unknown = target_ids - {group.scene_group_id for group in project.scene_groups}
        if unknown:
            raise ValueError(f"Unknown scene group(s): {sorted(unknown)}")
        shots_by_id = {shot.shot_id or shot.scene_id: shot for shot in project.shots}
        directory = job_directory(job_id)
        results: list[dict[str, Any]] = []
        target_groups = [group for group in project.scene_groups if group.scene_group_id in target_ids]
        hero_shots = [shots_by_id[group.hero_shot_id] for group in target_groups]
        timestamps = [(hero.start_time + hero.end_time) / 2.0 for hero in hero_shots]
        frames = sample_frames_at(project.source_path, timestamps, target_height=720)
        for group, hero, timestamp, frame in zip(target_groups, hero_shots, timestamps, frames):
            technical = self._complete(project.technical_recipe(hero))
            technical_lut = bake_3d_lut(
                technical, str(directory / "technical_inputs" / f"{group.scene_group_id}.cube"), self.ocio,
            )
            corrected = apply_cube_to_bgr(frame, technical_lut)
            model_result = generate_adaint_lut(
                corrected, str(directory / "scene_group_luts" / f"{group.scene_group_id}.cube"),
                size=int(params.get("lut_size", 33)),
            )
            # AdaInt output is a creative look, not a technical correction.
            lut_report = inspect_cube_lut(model_result["lut_path"], "creative")
            if not lut_report.passed:
                raise RuntimeError(f"AdaInt LUT failed QC for {group.scene_group_id}")
            group.creative_transform = TransformAsset(
                provider="adaint", asset_path=model_result["lut_path"],
                strength=float(params.get("strength", 1.0)), provider_version=model_result["model"],
                metadata={"hero_shot_id": hero.shot_id, "sample_time": timestamp, "lut_qc": lut_report.model_dump()},
            )
            group.approved_candidate_id = "adaint"
            results.append({
                "scene_group_id": group.scene_group_id, "hero_shot_id": hero.shot_id,
                "sample_time": timestamp, **model_result, "lut_qc": lut_report.model_dump(mode="json"),
            })
        project.revise(
            "generate_scene_group_adaint", params.get("actor", "local-user"),
            {"scene_group_ids": sorted(target_ids), "strength": params.get("strength", 1.0)},
        )
        write_json(directory / "project_recipe.json", project.model_dump(mode="json"))
        write_json(directory / "scene_group_adaint.json", {"groups": results})
        return {
            "project_recipe": project.model_dump(mode="json"), "revision": project.revision,
            "scene_group_luts": results, "scene_group_count": len(results),
        }

    def _handle_reference_candidates(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        from color_core.frame_sampler import sample_frames_at
        source_media = probe_media(params["source_path"])
        reference_media = probe_media(params["reference_path"])
        source_time = params.get("sample_time")
        if source_time is None:
            source_time = source_media.duration / 2.0 if source_media.is_video else 0.0
        reference_time = reference_media.duration / 2.0 if reference_media.is_video else 0.0
        source_times = params.get("source_times") or []
        project = None
        group_shots = []
        scoped_groups = []
        shots_by_id = {}
        affected_group_ids: list[str] = []
        if params.get("project_job_id"):
            project = self._project_from_job(params["project_job_id"])
            scope = params.get("scene_group_id") or "__all__"
            if scope == "__all__":
                scoped_groups = list(project.scene_groups)
            else:
                scoped_groups = [item for item in project.scene_groups if item.scene_group_id == scope]
                if not scoped_groups:
                    raise ValueError(f"Unknown scene group: {scope}")
            affected_group_ids = [item.scene_group_id for item in scoped_groups]
            shots_by_id = {shot.shot_id or shot.scene_id: shot for shot in project.shots}
            group_shots = [shots_by_id[item.hero_shot_id] for item in scoped_groups]
            source_times = [(shot.start_time + shot.end_time) / 2.0 for shot in group_shots]
        if not source_times:
            source_times = [float(source_time)]
        if project is not None and params.get("engine", "statistical") == "canoncgt":
            source_times = source_times[:1]
            group_shots = group_shots[:1]
        source_times = [float(value) for value in source_times[:9]]
        source_frames = sample_frames_at(params["source_path"], source_times)
        source_frame = source_frames[0]
        reference_frame = sample_frames_at(params["reference_path"], [float(reference_time)])[0]
        directory = job_directory(job_id)
        if project is not None and group_shots:
            corrected_frames = []
            technical_dir = directory / "technical_inputs"
            technical_dir.mkdir(parents=True, exist_ok=True)
            for index, (frame, shot) in enumerate(zip(source_frames, group_shots)):
                technical = self._complete(project.technical_recipe(shot))
                technical_lut = bake_3d_lut(
                    technical, str(technical_dir / f"{index:02d}.cube"), self.ocio,
                )
                corrected_frames.append(apply_cube_to_bgr(frame, technical_lut))
            source_frames = corrected_frames
            source_frame = source_frames[0]
        engine = params.get("engine", "statistical")
        reference_preflight = inspect_reference_frame(reference_frame)
        if not reference_preflight["passed"]:
            raise ValueError("Reference image failed preflight safety checks")
        if engine == "canoncgt":
            provider = CanonCGTProvider(self.ocio)
            if project is not None and scoped_groups:
                grouped_results: dict[str, list[dict[str, Any]]] = {}
                all_sample_times: list[float] = []
                for group in scoped_groups:
                    members = [shots_by_id[shot_id] for shot_id in group.shot_ids]
                    if len(members) >= 3:
                        indexes = [0, len(members) // 2, len(members) - 1]
                        sample_shots = [members[index] for index in indexes]
                        times = [(shot.start_time + shot.end_time) / 2.0 for shot in sample_shots]
                    elif len(members) == 2:
                        sample_shots = [members[0], members[1], members[1]]
                        times = [
                            (members[0].start_time + members[0].end_time) / 2.0,
                            members[1].start_time + (members[1].end_time - members[1].start_time) * 0.30,
                            members[1].start_time + (members[1].end_time - members[1].start_time) * 0.70,
                        ]
                    else:
                        shot = members[0]
                        sample_shots = [shot, shot, shot]
                        times = [shot.start_time + (shot.end_time - shot.start_time) * part for part in (0.2, 0.5, 0.8)]
                    frames = sample_frames_at(params["source_path"], times, target_height=720)
                    corrected_frames = []
                    for index, (frame, shot) in enumerate(zip(frames, sample_shots)):
                        technical_lut = bake_3d_lut(
                            self._complete(project.technical_recipe(shot)),
                            str(directory / "technical_inputs" / group.scene_group_id / f"{index:02d}.cube"), self.ocio,
                        )
                        corrected_frames.append(apply_cube_to_bgr(frame, technical_lut))
                    generated = provider.generate_candidates({
                        "source_frames": corrected_frames, "reference_frame": reference_frame,
                        "output_dir": str(directory / "scene_group_candidates" / group.scene_group_id),
                        "lut_size": int(params.get("lut_size", 33)),
                        "allow_fallback": bool(params.get("allow_fallback", True)),
                    })
                    grouped_results[group.scene_group_id] = [item.model_dump(mode="json") for item in generated]
                    all_sample_times.extend(times)

                candidates = []
                for candidate_index in range(3):
                    variants = [items[candidate_index] for items in grouped_results.values()]
                    base = dict(variants[0])
                    scores = [item.get("score") or {} for item in variants]
                    base["passed"] = all(bool(item.get("passed", True)) for item in variants)
                    base["fallback_used"] = any(bool(item.get("fallback_used")) for item in variants)
                    base["warnings"] = [
                        f"{group_id}: {warning}"
                        for group_id, items in grouped_results.items() for warning in items[candidate_index].get("warnings", [])
                    ]
                    base["score"] = {
                        key: round(sum(float(score.get(key, 0.0)) for score in scores) / len(scores), 6)
                        for key in ("technical_safety", "continuity", "reference_match", "total")
                    }
                    metadata = dict(base.get("metadata") or {})
                    metadata["group_luts"] = {
                        group_id: items[candidate_index]["lut_path"] for group_id, items in grouped_results.items()
                    }
                    metadata["group_scores"] = {
                        group_id: items[candidate_index].get("score", {}) for group_id, items in grouped_results.items()
                    }
                    metadata["group_metrics"] = {
                        group_id: items[candidate_index].get("metadata", {}) for group_id, items in grouped_results.items()
                    }
                    fit_values = [
                        float(item.get("metadata", {}).get("fit_rmse", 0.0))
                        for item in variants if item.get("metadata", {}).get("fit_rmse") is not None
                    ]
                    if fit_values:
                        metadata["fit_rmse"] = round(sum(fit_values) / len(fit_values), 8)
                    metadata["per_scene_group"] = True
                    base["metadata"] = metadata
                    candidates.append(base)
                source_times = all_sample_times
            else:
                generated = provider.generate_candidates({
                    "source_frames": source_frames, "reference_frame": reference_frame,
                    "output_dir": str(directory), "lut_size": int(params.get("lut_size", 33)),
                    "allow_fallback": bool(params.get("allow_fallback", True)),
                })
                candidates = [item.model_dump(mode="json") for item in generated]
        elif engine == "diffusion":
            source_image = directory / "source_keyframe.png"
            reference_image = directory / "reference_keyframe.png"
            if not cv2.imwrite(str(source_image), source_frame) or not cv2.imwrite(str(reference_image), reference_frame):
                raise RuntimeError("Could not persist model keyframes")
            candidates = []
            for variant in ("A", "B", "C"):
                model_result = run_reference_lut_model(
                    str(source_image), str(reference_image), str(directory / f"candidate_{variant.lower()}.cube"), variant,
                )
                candidates.append({"id": variant, "label": f"diffusion-{variant}", "engine": "diffusion", **model_result})
        else:
            candidates = generate_reference_candidates(source_frame, reference_frame, str(directory), self.ocio)
            for item in candidates:
                item["engine"] = "statistical"
        write_json(job_directory(job_id) / "reference_candidates.json", {
            "candidates": candidates, "reference_preflight": reference_preflight,
        })
        return {
            "candidates": candidates, "engine": engine, "source_sample_time": source_time,
            "source_sample_times": source_times, "reference_sample_time": reference_time,
            "reference_preflight": reference_preflight,
            "preview_scope": "whole_project" if project is not None else "whole_source",
            "affected_scene_group_ids": affected_group_ids,
        }

    def _handle_candidate_select(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        source = self._completed_job(params["candidate_job_id"], "Candidate")
        candidates = (source.get("result_data") or {}).get("candidates") or []
        selected = next((item for item in candidates if item.get("id") == params["candidate_id"]), None)
        if selected is None:
            raise ValueError(f"Candidate {params['candidate_id']} is not present")
        source_lut = Path(selected["lut_path"])
        destination = job_directory(job_id) / "selected.cube"
        shutil.copy2(source_lut, destination)
        selection = {**selected, "selected_lut_path": str(destination.resolve()), "source_candidate_job_id": params["candidate_job_id"]}
        if params.get("project_job_id") or params.get("scene_group_id"):
            if not params.get("project_job_id"):
                raise ValueError("project_job_id is required when applying a candidate to a scene group")
            project = self._project_from_job(params["project_job_id"])
            expected = params.get("expected_revision")
            if expected is None or project.revision != int(expected):
                raise ValueError(f"GradePlan revision conflict: expected {expected}, current {project.revision}")
            scope = params.get("scene_group_id") or "__all__"
            target_groups = (
                list(project.scene_groups) if scope == "__all__"
                else [item for item in project.scene_groups if item.scene_group_id == scope]
            )
            if not target_groups:
                raise ValueError(f"Unknown scene group: {scope}")
            score_data = selected.get("score") or {}
            fit_error = (selected.get("metadata") or {}).get("fit_rmse")
            group_luts = (selected.get("metadata") or {}).get("group_luts") or {}
            group_metrics = (selected.get("metadata") or {}).get("group_metrics") or {}
            for group in target_groups:
                selected_asset = destination
                if group.scene_group_id in group_luts:
                    selected_asset = job_directory(job_id) / "scene_group_luts" / f"{group.scene_group_id}.cube"
                    selected_asset.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(group_luts[group.scene_group_id]), selected_asset)
                group.creative_transform = TransformAsset(
                    provider=selected.get("provider") or selected.get("engine") or "statistical",
                    asset_path=str(selected_asset.resolve()), strength=1.0,
                    source_candidate_job_id=params["candidate_job_id"],
                    fit_error=(group_metrics.get(group.scene_group_id) or {}).get("fit_rmse", fit_error),
                    fallback_used=bool(selected.get("fallback_used", False)),
                    metadata={
                        "candidate_id": selected["id"], "candidate_strength": selected.get("strength", 1.0),
                        "score": score_data,
                    },
                )
                group.approved_candidate_id = selected["id"]
            project.workflow = "reference_assisted"
            project.revise(
                "select_reference_candidate", params.get("actor", "local-user"),
                {
                    "scene_group_ids": [group.scene_group_id for group in target_groups],
                    "candidate_id": selected["id"], "preview_scope": "whole_project",
                },
            )
            selection["project_recipe"] = project.model_dump(mode="json")
            selection["revision"] = project.revision
            selection["affected_scene_group_ids"] = [group.scene_group_id for group in target_groups]
            selection["preview_scope"] = "whole_project"
            write_json(job_directory(job_id) / "project_recipe.json", project.model_dump(mode="json"))
        write_json(job_directory(job_id) / "selection.json", selection)
        return selection

    def _handle_lut_render(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        source = params["input_path"]
        source_lut = Path(params["lut_path"])
        directory = job_directory(job_id)
        lut_path = directory / "input_lut.cube"
        shutil.copy2(source_lut, lut_path)
        media = probe_media(source)
        write_json(directory / "media_info.json", media.model_dump(mode="json"))
        preview = bool(params.get("preview", True))
        if preview:
            output = render_preview(
                source,
                str(lut_path),
                str(directory / "preview.mp4"),
                int(params.get("target_height", 720)),
                bool(params.get("split_screen", False)),
            )
        else:
            output = render_final(source, str(lut_path), str(directory / "graded_video.mp4"))
        qc = perform_quality_control(source, output)
        write_json(directory / "quality_report.json", qc.model_dump(mode="json"))
        if not qc.passed:
            raise RuntimeError("LUT render failed quality control: " + "; ".join(qc.errors))
        return {
            "lut_path": str(lut_path.resolve()),
            "preview": preview,
            "preview_path": output if preview else None,
            "output_path": output if not preview else None,
            "quality_report": qc.model_dump(mode="json"),
        }


job_worker_instance = JobWorker()

"""Immutable approval bundles for reproducible GradePlan rendering."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path
from typing import Any

from color_core.project_recipe import ProjectGradeRecipe
from color_core.provenance import compute_dict_sha256, compute_file_sha256


def _materialize(source: Path, destination: Path) -> str:
    source = source.resolve()
    if not source.is_file():
        raise ValueError(f"Approval asset is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
        method = "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        method = "copy"
    return method


def _asset_record(path: Path, relative_path: str, kind: str, method: str) -> dict[str, Any]:
    return {
        "artifact_id": f"{kind}_{compute_file_sha256(str(path))[:16]}",
        "kind": kind,
        "relative_path": relative_path.replace("\\", "/"),
        "sha256": compute_file_sha256(str(path)),
        "size_bytes": path.stat().st_size,
        "materialization": method,
    }


def freeze_approved_revision(project: ProjectGradeRecipe, bundle_path: str | Path) -> dict[str, Any]:
    """Freeze source media and selected transforms, mutate references, and write a manifest."""
    if project.status != "approved":
        raise ValueError("Only an approved GradePlan can be frozen")
    bundle = Path(bundle_path).resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    source = Path(project.source_path).resolve()
    source_relative = f"media/source{source.suffix.lower()}"
    frozen_source = bundle / source_relative
    source_method = _materialize(source, frozen_source)
    source_record = _asset_record(frozen_source, source_relative, "source", source_method)
    records.append(source_record)
    project.source_path = str(frozen_source)
    project.source_media_hash = source_record["sha256"]
    project.source_artifact_id = source_record["artifact_id"]

    for group in project.scene_groups:
        transform = group.creative_transform
        if transform is None:
            group.creative_source = "project_look_inherited"
            group.approval_state = "approved_by_project"
            continue
        source_path = Path(project.resolve_transform_path(transform)).resolve()
        suffix = source_path.suffix.lower() or ".cube"
        relative = f"assets/{group.scene_group_id}_{transform.provider}{suffix}"
        frozen = bundle / relative
        method = _materialize(source_path, frozen)
        record = _asset_record(frozen, relative, "transform", method)
        record.update({
            "scene_group_id": group.scene_group_id,
            "provider": transform.provider,
            "provider_version": transform.provider_version,
            "transform_type": transform.transform_type,
        })
        records.append(record)
        transform.artifact_id = record["artifact_id"]
        transform.sha256 = record["sha256"]
        transform.relative_path = record["relative_path"]
        transform.asset_path = str(frozen)
        group.creative_source = "provider_fallback" if transform.fallback_used else "provider_candidate"
        group.approval_state = "approved_candidate"
        group.fallback_used = transform.fallback_used

    project.approval_bundle_path = str(bundle)
    manifest: dict[str, Any] = {
        "manifest_version": "1.0",
        "recipe_version": project.recipe_version,
        "revision": project.revision,
        "approved_at": project.approved_at,
        "approved_by": project.approved_by,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "assets": records,
        "recipe_relative_path": "recipe.json",
    }
    manifest["manifest_sha256"] = compute_dict_sha256(manifest)
    project.approval_manifest = manifest
    (bundle / "recipe.json").write_text(
        json.dumps(project.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def verify_approval_bundle(bundle_path: str | Path) -> dict[str, Any]:
    bundle = Path(bundle_path).resolve()
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        return {"valid": False, "errors": ["manifest.json is missing"], "assets": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_manifest_hash = manifest.pop("manifest_sha256", "")
    errors: list[str] = []
    if compute_dict_sha256(manifest) != expected_manifest_hash:
        errors.append("manifest hash does not match")
    results: list[dict[str, Any]] = []
    for record in manifest.get("assets", []):
        path = bundle / record["relative_path"]
        actual = compute_file_sha256(str(path)) if path.is_file() else ""
        valid = bool(actual) and actual == record.get("sha256")
        results.append({"artifact_id": record.get("artifact_id"), "valid": valid, "path": str(path)})
        if not valid:
            errors.append(f"asset hash mismatch or missing: {record.get('relative_path')}")
    return {"valid": not errors, "errors": errors, "assets": results}

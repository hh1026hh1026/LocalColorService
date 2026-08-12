"""Build a browser-safe inventory of source and rendered comparison videos."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
OUTPUT_FILES = {
    "preview.mp4": ("preview", "预览结果"),
    "graded_video.mp4": ("render", "正式结果"),
    "project_preview.mp4": ("preview", "镜头项目预览"),
    "timeline_preview.mp4": ("preview", "时间线选段预览"),
    "project_graded_video.mp4": ("render", "镜头项目正式结果"),
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid_video(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and path.suffix.lower() in VIDEO_EXTENSIONS
    except OSError:
        return False


def _resolved_key(path: Path) -> str:
    resolved = str(path.resolve()).casefold().encode("utf-8")
    return hashlib.sha256(resolved).hexdigest()[:16]


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _recipe_summary(recipe: dict[str, Any]) -> str | None:
    if not recipe:
        return None
    try:
        exposure = float(recipe.get("exposure", 0.0))
        contrast = float(recipe.get("contrast", 1.0))
        saturation = float(recipe.get("saturation", 1.0))
    except (TypeError, ValueError):
        return None
    return f"EV {exposure:+.2f} · 对比 {contrast:.2f} · 饱和 {saturation:.2f}"


def _source_path(request: dict[str, Any]) -> Path | None:
    raw_path = request.get("input_path") or request.get("file_path") or request.get("source_path")
    return Path(raw_path) if isinstance(raw_path, str) and raw_path.strip() else None


def build_comparison_inventory(data_dir: Path) -> list[dict[str, Any]]:
    """Return playable uploads, task sources, previews, and final renders."""
    data_dir = data_dir.resolve()
    uploads_dir = data_dir / "uploads"
    jobs_dir = data_dir / "jobs"
    items: list[dict[str, Any]] = []
    seen_sources: set[str] = set()

    if uploads_dir.is_dir():
        for path in uploads_dir.iterdir():
            if not _valid_video(path):
                continue
            group_key = _resolved_key(path)
            seen_sources.add(group_key)
            items.append({
                "id": f"upload-{group_key}",
                "label": f"原片 · {path.name}",
                "url": f"/data/view/uploads/{path.name}",
                "kind": "source",
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "modified_at": _iso_mtime(path),
                "group_key": group_key,
                "job_id": None,
                "recipe_summary": None,
            })

    if not jobs_dir.is_dir():
        return sorted(items, key=lambda item: item["modified_at"], reverse=True)

    job_dirs = sorted(
        (path for path in jobs_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for job_dir in job_dirs:
        request = _read_json(job_dir / "request.json")
        source = _source_path(request)
        source_group = _resolved_key(source) if source and _valid_video(source) else None
        recipe = _read_json(job_dir / "grade_recipe.json")
        summary = _recipe_summary(recipe)

        output_found = False
        for filename, (kind, title) in OUTPUT_FILES.items():
            output = job_dir / filename
            if not _valid_video(output):
                continue
            output_found = True
            group_key = source_group or _resolved_key(output)
            suffix = f" · {summary}" if summary else ""
            items.append({
                "id": f"{kind}-{job_dir.name}-{filename}",
                "label": f"{title} · {job_dir.name}{suffix}",
                "url": f"/data/view/jobs/{job_dir.name}/{filename}",
                "kind": kind,
                "filename": filename,
                "size_bytes": output.stat().st_size,
                "modified_at": _iso_mtime(output),
                "group_key": group_key,
                "job_id": job_dir.name,
                "recipe_summary": summary,
            })

        if output_found and source and source_group and source_group not in seen_sources:
            seen_sources.add(source_group)
            items.append({
                "id": f"source-{job_dir.name}",
                "label": f"原片 · {source.name}",
                "url": f"/v1/comparison/jobs/{job_dir.name}/source",
                "kind": "source",
                "filename": source.name,
                "size_bytes": source.stat().st_size,
                "modified_at": _iso_mtime(source),
                "group_key": source_group,
                "job_id": job_dir.name,
                "recipe_summary": None,
            })

    kind_order = {"source": 0, "preview": 1, "render": 2}
    return sorted(
        items,
        key=lambda item: (item["modified_at"], -kind_order[item["kind"]]),
        reverse=True,
    )


def resolve_job_source(data_dir: Path, job_id: str) -> Path | None:
    """Resolve a source only through an existing job request record."""
    if not job_id or any(char not in "0123456789abcdefABCDEF" for char in job_id):
        return None
    request_path = data_dir.resolve() / "jobs" / job_id / "request.json"
    if not request_path.is_file():
        return None
    source = _source_path(_read_json(request_path))
    return source.resolve() if source and _valid_video(source) else None

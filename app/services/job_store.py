"""Filesystem artifact storage for isolated jobs."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from app.config import settings


def job_directory(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef" for ch in job_id.casefold()):
        raise ValueError("Invalid job id")
    return settings.DATA_DIR / "jobs" / job_id


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)
    return path


def initialize_job(job_id: str, request: dict[str, Any]) -> Path:
    directory = job_directory(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "request.json", request)
    append_log(job_id, "job created")
    return directory


def append_log(job_id: str, message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    log_path = job_directory(job_id) / "task.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"{timestamp} {message}\n")


def append_event(job_id: str, event: str, details: dict[str, Any] | None = None) -> None:
    """Append a machine-readable operational event alongside the human task log."""
    record = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        "details": details or {},
    }
    path = job_directory(job_id) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def artifact_map(job_id: str) -> dict[str, str]:
    directory = job_directory(job_id)
    if not directory.exists():
        return {}
    return {item.name: str(item.resolve()) for item in directory.iterdir() if item.is_file()}

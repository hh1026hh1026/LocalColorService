"""Cooperative job cancellation (V0.6.3).

Until now a job could not be stopped. A fifteen-minute render started by mistake
had to either run to completion or be killed by restarting the whole service,
which also dropped every other queued job. That is not an acceptable cost for a
misclick.

How it works
------------
The API and the workers live in the same process, so cancellation does not need
to go through the database to take effect. Every subprocess a job spawns is
registered against that job's id; cancelling the job terminates them and raises
:class:`JobCancelled` inside the worker thread, which records the job as
``cancelled`` rather than ``failed``.

Long in-process loops (face-mask generation, per-shot LUT baking) call
:func:`raise_if_cancelled` between iterations so they stop promptly too, instead
of only at the next subprocess boundary.
"""

from __future__ import annotations

import contextvars
import subprocess
import threading
import time
from typing import Any, Optional

__all__ = [
    "JobCancelled",
    "set_current_job",
    "current_job",
    "request_cancel",
    "is_cancelled",
    "raise_if_cancelled",
    "release",
    "run",
]


class JobCancelled(RuntimeError):
    """Raised inside a worker when its job has been cancelled."""


_current_job: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "local_color_current_job", default=None
)
_lock = threading.RLock()
_processes: dict[str, set[subprocess.Popen]] = {}
_cancelled: set[str] = set()


def set_current_job(job_id: Optional[str]) -> None:
    """Bind the calling worker thread to a job id.

    Each worker is its own thread with its own context, so the two lanes never
    see each other's job id.
    """
    _current_job.set(job_id)


def current_job() -> Optional[str]:
    return _current_job.get()


def is_cancelled(job_id: Optional[str] = None) -> bool:
    target = job_id or current_job()
    if not target:
        return False
    with _lock:
        return target in _cancelled


def raise_if_cancelled(job_id: Optional[str] = None) -> None:
    target = job_id or current_job()
    if target and is_cancelled(target):
        raise JobCancelled(f"Job {target} was cancelled")


def release(job_id: str) -> None:
    """Forget a job once the worker has finished with it."""
    with _lock:
        _cancelled.discard(job_id)
        _processes.pop(job_id, None)


def _register(job_id: str, process: subprocess.Popen) -> None:
    with _lock:
        _processes.setdefault(job_id, set()).add(process)


def _unregister(job_id: str, process: subprocess.Popen) -> None:
    with _lock:
        group = _processes.get(job_id)
        if group:
            group.discard(process)
            if not group:
                _processes.pop(job_id, None)


def request_cancel(job_id: str, grace_seconds: float = 3.0) -> dict[str, Any]:
    """Mark a job cancelled and terminate anything it is currently running."""
    with _lock:
        _cancelled.add(job_id)
        processes = [item for item in _processes.get(job_id, set())]

    terminated = 0
    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
                terminated += 1
        except Exception:
            pass
    if terminated:
        deadline = time.monotonic() + grace_seconds
        for process in processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except Exception:
                # FFmpeg occasionally ignores SIGTERM mid-write; escalate.
                try:
                    process.kill()
                except Exception:
                    pass
    return {"job_id": job_id, "terminated_processes": terminated}


def run(
    command: list[str],
    *,
    timeout: Optional[float] = None,
    job_id: Optional[str] = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` that can be cancelled.

    Drop-in for the ``capture_output=True, text=True`` calls the renderers make.
    Without the registration step a cancel request could only take effect after
    the current ffmpeg invocation had finished on its own - which for a full
    project render is the entire job.
    """
    target = job_id or current_job()
    if target:
        raise_if_cancelled(target)
    if not target:
        return subprocess.run(command, timeout=timeout, **kwargs)

    capture = kwargs.pop("capture_output", False)
    text = kwargs.pop("text", False)
    encoding = kwargs.pop("encoding", None)
    errors = kwargs.pop("errors", None)
    kwargs.pop("check", None)
    if capture:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)

    process = subprocess.Popen(
        command, text=text, encoding=encoding, errors=errors, **kwargs
    )
    _register(target, process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise
    finally:
        _unregister(target, process)

    if is_cancelled(target):
        raise JobCancelled(f"Job {target} was cancelled")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

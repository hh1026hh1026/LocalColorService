"""Live progress from a running FFmpeg render.

A project render reported ``progress = 5`` from the moment it was claimed until
the moment it finished - six and a half minutes of a bar that never moved, which
looks exactly like a hung job. There is no way for the caller to tell "still
encoding" from "stuck", and that ambiguity is what made a fifteen-minute render
feel like a failure in the first place.

FFmpeg will write machine-readable progress to a file with ``-progress``. Doing
it via a file rather than a pipe avoids any interaction with the stdout/stderr
capture the cancellable runner already performs - no deadlock risk, and nothing
to unwind if the process is killed mid-write.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

__all__ = ["progress_arguments", "FFmpegProgressMonitor"]


def progress_arguments(progress_path: str | Path) -> list[str]:
    """FFmpeg arguments that emit progress to ``progress_path``."""
    return ["-progress", str(Path(progress_path).resolve()), "-nostats"]


def _parse_tail(text: str) -> dict[str, str]:
    """Last complete progress block from FFmpeg's key=value stream."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


class FFmpegProgressMonitor:
    """Polls FFmpeg's progress file and reports a 0-1 fraction.

    Used as a context manager; the polling thread stops on exit regardless of
    how the render ended.
    """

    def __init__(
        self,
        progress_path: str | Path,
        total_frames: int = 0,
        duration_seconds: float = 0.0,
        on_progress: Optional[Callable[[float], None]] = None,
        poll_interval: float = 1.0,
    ):
        self.path = Path(progress_path)
        self.total_frames = int(total_frames or 0)
        self.duration_us = float(duration_seconds or 0.0) * 1e6
        self.on_progress = on_progress
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fraction = 0.0

    def _read_fraction(self) -> Optional[float]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        values = _parse_tail(text)
        if not values:
            return None
        if self.total_frames > 0 and values.get("frame", "").isdigit():
            return min(1.0, int(values["frame"]) / self.total_frames)
        if self.duration_us > 0:
            raw = values.get("out_time_us") or values.get("out_time_ms")
            try:
                # out_time_ms is documented in microseconds despite the name.
                return min(1.0, float(raw) / self.duration_us)
            except (TypeError, ValueError):
                return None
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            fraction = self._read_fraction()
            if fraction is not None and fraction > self.fraction:
                self.fraction = fraction
                if self.on_progress:
                    try:
                        self.on_progress(fraction)
                    except Exception:
                        # Progress reporting must never take a render down.
                        pass
            self._stop.wait(self.poll_interval)

    def __enter__(self) -> "FFmpegProgressMonitor":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
        except OSError:
            pass
        self._thread = threading.Thread(target=self._run, name="FFmpegProgress", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

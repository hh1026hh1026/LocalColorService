"""V0.6.7 - make a defect look different from an expected fallback, and make a
long render look different from a stuck one.

Both of these cost real time. A ``TypeError`` from the V0.6.2 chained-LUT change
was caught by the same handler that deals with a missing encoder, so four
consecutive renders silently skipped face protection and it was only found by
reading logs days later. Separately, ``project_render`` reported 5% for its
entire six-and-a-half-minute duration, which is indistinguishable from hung.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import pytest

from app.services.database import DatabaseManager
from color_core.degradation import (
    describe_degradation,
    is_programming_error,
    report_degradation,
)
from color_core.render_progress import FFmpegProgressMonitor, progress_arguments


# ---------------------------------------------------------------------------
# Defect vs environment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error",
    [TypeError("expected str, not list"), AttributeError("no attribute"), KeyError("k"),
     IndexError("out of range"), NameError("undefined")],
)
def test_programming_errors_are_recognised(error):
    assert is_programming_error(error)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("encoder unavailable"), OSError("file busy"),
     subprocess.SubprocessError("ffmpeg failed"), ValueError("bad input")],
)
def test_environmental_errors_are_not_flagged_as_defects(error):
    assert not is_programming_error(error)


def test_the_exact_production_failure_is_classified_as_a_defect():
    """The face-selective regression, verbatim."""
    error = TypeError("expected str, bytes or os.PathLike object, not list")
    record = describe_degradation("Face-selective render", error)
    assert record["cause"] == "defect"
    assert record["severity"] == "error"


def test_a_defect_carries_a_traceback_and_an_expected_fallback_does_not():
    try:
        raise TypeError("interface mismatch")
    except TypeError as error:
        assert describe_degradation("f", error)["traceback"]
    try:
        raise RuntimeError("no encoder")
    except RuntimeError as error:
        assert describe_degradation("f", error)["traceback"] == ""


def test_report_degradation_logs_at_the_right_level(caplog):
    logger = logging.getLogger("test_degradation")
    with caplog.at_level(logging.WARNING, logger="test_degradation"):
        report_degradation(logger, "Feature", RuntimeError("no encoder"))
    assert caplog.records[-1].levelno == logging.WARNING

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="test_degradation"):
        report_degradation(logger, "Feature", TypeError("not list"))
    assert caplog.records[-1].levelno == logging.ERROR
    assert "DEFECT" in caplog.records[-1].message


# ---------------------------------------------------------------------------
# Render progress
# ---------------------------------------------------------------------------

def test_progress_arguments_request_a_machine_readable_stream(tmp_path):
    args = progress_arguments(tmp_path / "p.txt")
    assert "-progress" in args
    assert "-nostats" in args


def test_monitor_reports_a_rising_fraction(tmp_path):
    path = tmp_path / "p.txt"
    seen: list[float] = []
    with FFmpegProgressMonitor(
        path, total_frames=100, on_progress=seen.append, poll_interval=0.05
    ):
        for frame in (10, 40, 90):
            path.write_text(f"frame={frame}\nfps=25\nprogress=continue\n", encoding="utf-8")
            time.sleep(0.18)
    assert seen, "no progress was reported"
    assert seen == sorted(seen), "progress must not go backwards"
    assert seen[-1] == pytest.approx(0.9, abs=0.01)


def test_monitor_falls_back_to_time_when_frame_count_is_unknown(tmp_path):
    path = tmp_path / "p.txt"
    seen: list[float] = []
    with FFmpegProgressMonitor(
        path, total_frames=0, duration_seconds=10.0, on_progress=seen.append, poll_interval=0.05
    ):
        path.write_text("out_time_us=5000000\nprogress=continue\n", encoding="utf-8")
        time.sleep(0.2)
    assert seen and seen[-1] == pytest.approx(0.5, abs=0.02)


def test_monitor_survives_a_missing_or_empty_file(tmp_path):
    seen: list[float] = []
    with FFmpegProgressMonitor(
        tmp_path / "never_written.txt", total_frames=100, on_progress=seen.append,
        poll_interval=0.05,
    ):
        time.sleep(0.15)
    assert seen == []


def test_a_failing_callback_cannot_break_the_render(tmp_path):
    path = tmp_path / "p.txt"

    def explode(_fraction: float) -> None:
        raise RuntimeError("progress sink is down")

    with FFmpegProgressMonitor(path, total_frames=100, on_progress=explode, poll_interval=0.05):
        path.write_text("frame=50\n", encoding="utf-8")
        time.sleep(0.15)
    # Reaching here without an exception escaping the monitor is the assertion.


def test_progress_file_is_cleaned_up(tmp_path):
    path = tmp_path / "p.txt"
    with FFmpegProgressMonitor(path, total_frames=10, poll_interval=0.05):
        path.write_text("frame=5\n", encoding="utf-8")
        time.sleep(0.1)
    assert not path.exists()


# ---------------------------------------------------------------------------
# Persisted progress
# ---------------------------------------------------------------------------

def test_progress_updates_only_move_forward(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "jobs.db"))
    db.create_job("j", "project_render", {})
    db.fetch_next_pending_job()
    db.update_job_progress("j", 40)
    assert db.get_job("j")["progress"] == 40
    db.update_job_progress("j", 20)
    assert db.get_job("j")["progress"] == 40, "progress must not go backwards"


def test_progress_never_reaches_100_before_completion(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "jobs.db"))
    db.create_job("j", "project_render", {})
    db.fetch_next_pending_job()
    db.update_job_progress("j", 250)
    assert db.get_job("j")["progress"] == 99
    assert db.get_job("j")["status"] == "processing"


def test_progress_is_ignored_for_a_job_that_is_not_running(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "jobs.db"))
    db.create_job("j", "project_render", {})
    db.update_job_progress("j", 50)
    assert db.get_job("j")["progress"] == 0

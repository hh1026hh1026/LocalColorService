"""V0.6.3 job cancellation.

Before this a job could not be stopped. A fifteen-minute render started by
mistake had to run to completion or be killed by restarting the service, which
dropped every other queued job with it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services.database import DatabaseManager
from color_core.cancellation import (
    JobCancelled,
    is_cancelled,
    raise_if_cancelled,
    release,
    request_cancel,
    run as cancellable_run,
    set_current_job,
)


@pytest.fixture
def db(tmp_path) -> DatabaseManager:
    return DatabaseManager(db_path=str(tmp_path / "jobs.db"))


@pytest.fixture(autouse=True)
def _clean_context():
    set_current_job(None)
    yield
    set_current_job(None)


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------

def test_cancel_flag_is_scoped_to_a_job():
    request_cancel("job-a")
    assert is_cancelled("job-a")
    assert not is_cancelled("job-b")
    release("job-a")
    assert not is_cancelled("job-a")


def test_raise_if_cancelled_uses_the_current_job():
    set_current_job("job-c")
    raise_if_cancelled()  # not cancelled yet
    request_cancel("job-c")
    with pytest.raises(JobCancelled):
        raise_if_cancelled()
    release("job-c")


def test_run_without_a_job_context_behaves_like_subprocess_run():
    result = cancellable_run(
        [sys.executable, "-c", "print('hello')"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_returns_output_under_a_job_context():
    set_current_job("job-d")
    try:
        result = cancellable_run(
            [sys.executable, "-c", "print('captured')"], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "captured" in result.stdout
    finally:
        release("job-d")


def test_run_refuses_to_start_once_cancelled():
    set_current_job("job-e")
    request_cancel("job-e")
    try:
        with pytest.raises(JobCancelled):
            cancellable_run([sys.executable, "-c", "pass"], capture_output=True, text=True)
    finally:
        release("job-e")


def test_a_running_subprocess_is_actually_killed():
    """The point of the whole mechanism: stop work already in flight."""
    set_current_job("job-f")
    outcome: dict = {}

    def worker():
        try:
            cancellable_run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                capture_output=True, text=True, job_id="job-f",
            )
            outcome["result"] = "completed"
        except JobCancelled:
            outcome["result"] = "cancelled"
        except Exception as exc:  # pragma: no cover
            outcome["result"] = f"error: {exc}"

    thread = threading.Thread(target=worker)
    started = time.monotonic()
    thread.start()
    time.sleep(0.6)  # let the child actually start
    report = request_cancel("job-f")
    thread.join(timeout=15)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "worker did not stop"
    assert outcome["result"] == "cancelled"
    assert report["terminated_processes"] >= 1
    assert elapsed < 15, "cancellation should be prompt, not wait out the child"
    release("job-f")


# ---------------------------------------------------------------------------
# Database behaviour
# ---------------------------------------------------------------------------

def test_pending_job_is_cancelled_immediately(db):
    db.create_job("p1", "project_render", {})
    outcome = db.cancel_job("p1")
    assert outcome["status"] == "cancelled"
    assert db.get_job("p1")["status"] == "cancelled"


def test_a_cancelled_job_is_never_picked_up(db):
    db.create_job("p1", "project_render", {})
    db.cancel_job("p1")
    assert db.fetch_next_pending_job() is None


def test_cancelling_a_finished_job_changes_nothing(db):
    db.create_job("p1", "recipe", {})
    db.update_job_status("p1", "completed", 100, result_data={"ok": True})
    outcome = db.cancel_job("p1")
    assert outcome["changed"] is False
    assert db.get_job("p1")["status"] == "completed"


def test_running_job_immediately_enters_cancelling_state(db):
    db.create_job("running", "project_render", {})
    db.fetch_next_pending_job(worker_id="worker-a")
    outcome = db.cancel_job("running")
    assert outcome["status"] == "cancelling"
    assert db.get_job("running")["status"] == "cancelling"


def test_cancelling_a_missing_job_reports_not_found(db):
    assert db.cancel_job("nope")["found"] is False


def test_cancel_all_covers_pending_and_running(db):
    db.create_job("a", "project_render", {})
    db.create_job("b", "scenes", {})
    db.create_job("c", "reference_preflight", {})
    db.fetch_next_pending_job()  # 'a' starts
    outcome = db.cancel_all_active()
    assert len(outcome["requested"]) == 3
    assert db.get_job("b")["status"] == "cancelled"
    assert db.get_job("c")["status"] == "cancelled"
    release("a")


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

def test_purge_removes_records_and_directories(db, tmp_path):
    db.create_job("done", "recipe", {})
    db.update_job_status("done", "completed", 100, result_data={})
    directory = tmp_path / "jobs" / "done"
    directory.mkdir(parents=True)
    (directory / "artifact.bin").write_bytes(b"x" * 2048)

    result = db.purge_jobs()
    assert result["purged"] == 1
    assert result["freed_bytes"] >= 2048
    assert not directory.exists()
    assert db.get_job("done") is None


def test_purge_one_job_does_not_delete_other_history(db, tmp_path):
    for job_id in ("one", "two"):
        db.create_job(job_id, "recipe", {})
        db.update_job_status(job_id, "completed", 100, result_data={})
        directory = tmp_path / "jobs" / job_id
        directory.mkdir(parents=True)
    assert db.purge_job("one")
    assert db.get_job("one") is None
    assert db.get_job("two") is not None
    assert (tmp_path / "jobs" / "two").is_dir()


def test_purge_refuses_active_statuses(db):
    with pytest.raises(ValueError, match="Cancel a job"):
        db.purge_jobs(statuses=["pending"])


def test_purge_leaves_active_jobs_alone(db):
    db.create_job("busy", "project_render", {})
    db.create_job("old", "recipe", {})
    db.update_job_status("old", "failed", 100, error_message="x")
    result = db.purge_jobs()
    assert result["job_ids"] == ["old"]
    assert db.get_job("busy") is not None


def test_purge_age_filter(db):
    db.create_job("recent", "recipe", {})
    db.update_job_status("recent", "completed", 100, result_data={})
    # Nothing is a week old yet.
    assert db.purge_jobs(older_than_days=7)["purged"] == 0
    assert db.purge_jobs(older_than_days=0)["purged"] == 1

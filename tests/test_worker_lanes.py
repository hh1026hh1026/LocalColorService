"""Two-lane job queue.

The bug this fixes was observed in production logs: a CanonCGT reference
preflight - a job that takes about one second - sat in 'pending' indefinitely
because a face-selective project_render had been running for fifteen minutes on
the single worker. From the UI the queued job was indistinguishable from a
failed image upload.
"""

from __future__ import annotations

import pytest

from app.services.database import DatabaseManager
from workers.job_worker import LIGHT_JOB_TYPES, lane_for_job_type


@pytest.fixture
def db(tmp_path) -> DatabaseManager:
    return DatabaseManager(db_path=str(tmp_path / "jobs.db"))


def test_lane_classification():
    assert lane_for_job_type("reference_preflight") == "light"
    assert lane_for_job_type("project_recipe") == "light"
    assert lane_for_job_type("project_render") == "heavy"
    assert lane_for_job_type("reference_candidates") == "heavy"
    assert lane_for_job_type("scenes") == "heavy"


def test_unknown_job_types_are_treated_as_heavy():
    """Conservative default: an unknown job must never occupy the fast lane."""
    assert lane_for_job_type("some_future_job") == "heavy"


def test_light_job_is_claimed_while_a_render_is_queued_ahead_of_it(db):
    """The exact production scenario, in order."""
    db.create_job("render01", "project_render", {})
    db.create_job("preflight01", "reference_preflight", {})

    heavy = db.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES))
    assert heavy["job_id"] == "render01"

    # The render is now 'processing'. The light lane must still find the
    # preflight rather than waiting behind it.
    light = db.fetch_next_pending_job(include_types=list(LIGHT_JOB_TYPES))
    assert light is not None, "preflight was blocked behind the render"
    assert light["job_id"] == "preflight01"


def test_lanes_never_claim_each_others_work(db):
    db.create_job("r1", "project_render", {})
    assert db.fetch_next_pending_job(include_types=list(LIGHT_JOB_TYPES)) is None
    db.create_job("p1", "reference_preflight", {})
    assert db.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES))["job_id"] == "r1"
    assert db.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES)) is None


def test_a_job_is_claimed_exactly_once(db):
    db.create_job("only", "reference_preflight", {})
    first = db.fetch_next_pending_job(include_types=list(LIGHT_JOB_TYPES))
    second = db.fetch_next_pending_job(include_types=list(LIGHT_JOB_TYPES))
    assert first["job_id"] == "only"
    assert second is None


def test_fifo_order_is_preserved_within_a_lane(db):
    for index in range(3):
        db.create_job(f"j{index}", "reference_preflight", {})
    order = [
        db.fetch_next_pending_job(include_types=list(LIGHT_JOB_TYPES))["job_id"]
        for _ in range(3)
    ]
    assert order == ["j0", "j1", "j2"]


def test_queue_snapshot_reports_position_and_blocker(db):
    """Waiting must be reported as waiting, not as an unexplained 'pending'."""
    db.create_job("render01", "project_render", {})
    db.create_job("render02", "project_render", {})
    db.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES))  # render01 starts

    snapshot = db.queue_snapshot("render02")
    assert snapshot["lane"] == "heavy"
    assert snapshot["queue_position"] == 2
    assert snapshot["blocked_by"]["job_id"] == "render01"


def test_queue_snapshot_for_a_light_job_ignores_the_heavy_lane(db):
    db.create_job("render01", "project_render", {})
    db.create_job("preflight01", "reference_preflight", {})
    db.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES))

    snapshot = db.queue_snapshot("preflight01")
    assert snapshot["lane"] == "light"
    # First in its own lane; the running render is irrelevant to it.
    assert snapshot["queue_position"] == 1
    assert snapshot["blocked_by"] is None


def test_queue_snapshot_of_a_running_job(db):
    db.create_job("render01", "project_render", {})
    db.fetch_next_pending_job(exclude_types=list(LIGHT_JOB_TYPES))
    assert db.queue_snapshot("render01")["queue_position"] == 0


def test_queue_snapshot_of_a_missing_job_is_empty(db):
    assert db.queue_snapshot("nope") == {}

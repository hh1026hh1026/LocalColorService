from pathlib import Path
import datetime

from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.quality_control import perform_quality_control
from color_core.recipe import GradeRecipe
from color_core.renderer import render_final


ASSETS = Path(__file__).parents[1] / "test_assets"


def test_video_acceptance_contract(tmp_path):
    source = str(ASSETS / "neutral_sample.mp4")
    manager = OCIOManager()
    lut = bake_3d_lut(GradeRecipe(), str(tmp_path / "neutral.cube"), manager)
    output = render_final(source, lut, str(tmp_path / "graded.mp4"))
    report = perform_quality_control(source, output)
    assert report.passed
    assert report.output_readable and report.output_nonempty
    assert report.duration_within_one_frame and report.fps_matches
    assert report.audio_preserved and report.metadata_ok


def test_failed_job_persists_error(tmp_path):
    from app.services.database import DatabaseManager

    database = DatabaseManager(str(tmp_path / "jobs.db"))
    database.create_job("deadbeef", "render", {"input_path": "missing.mp4"})
    database.update_job_status("deadbeef", "failed", 100, error_message="Source file not found")
    failed = database.get_job("deadbeef")
    assert failed["status"] == "failed"
    assert "not found" in failed["error_message"]


def test_atomic_claim_and_interrupted_recovery(tmp_path):
    from app.services.database import DatabaseManager

    database = DatabaseManager(str(tmp_path / "local_color.db"))
    directory = tmp_path / "jobs" / "cafe1234"
    directory.mkdir(parents=True)
    (directory / "request.json").write_text("{}", encoding="utf-8")
    database.create_job("cafe1234", "analyze", {"file_path": "sample.mp4"})
    claimed = database.fetch_next_pending_job()
    assert claimed["status"] == "processing"
    assert database.fetch_next_pending_job() is None
    # A healthy worker holds a lease and must not be stolen during recovery.
    assert database.recover_interrupted_jobs() == {"requeued": 0, "failed": 0, "cancelled": 0}
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE job_id=?",
            ((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)).isoformat(), "cafe1234"),
        )
        conn.commit()
    assert database.recover_interrupted_jobs() == {"requeued": 1, "failed": 0, "cancelled": 0}
    assert database.get_job("cafe1234")["status"] == "pending"

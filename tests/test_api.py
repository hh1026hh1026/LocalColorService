import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.database import db_manager


ASSETS = Path(__file__).parents[1] / "test_assets"


def _complete(client: TestClient, job_id: str):
    for _ in range(40):
        time.sleep(0.1)
        job = db_manager.get_job(job_id)
        if job and job["status"] in ["completed", "failed"]:
            break
    response = client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def test_api_models_and_analyze_recipe_lut():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/version").json()["ocio_config"].startswith("studio-config-")
        assert client.get("/docs").status_code == 200
        source = str((ASSETS / "neutral_sample.mp4").resolve())
        analyze = client.post("/v1/color/analyze", json={"file_path": source, "sample_count": 3}).json()
        analyzed = _complete(client, analyze["job_id"])
        assert analyzed["status"] == "completed"
        recipe = client.post("/v1/color/recipe", json={"analysis_job_id": analyze["job_id"], "exposure": 0.1}).json()
        made = _complete(client, recipe["job_id"])
        assert made["result_data"]["recipe"]["exposure"] == 0.1
        lut = client.post("/v1/color/lut", json={"recipe_job_id": recipe["job_id"]}).json()
        baked = _complete(client, lut["job_id"])
        assert Path(baked["result_data"]["lut_path"]).is_file()
        report = client.get(f"/v1/jobs/{lut['job_id']}/report").json()
        assert "output.cube" in report["artifacts"]


def test_task_board_lists_jobs_without_result_payloads():
    with TestClient(app) as client:
        db_manager.create_job("board-test-job", "recipe", {"analysis_job_id": "source"})
        response = client.get("/v1/jobs?limit=10")
        assert response.status_code == 200
        item = next(job for job in response.json()["jobs"] if job["job_id"] == "board-test-job")
        assert item["status"] in ("pending", "processing", "completed", "failed")
        assert "result_data" not in item
        assert item["execution"]["mode"]
        assert client.get("/tasks").status_code == 200


def test_task_detail_exposes_result_and_qc_summary():
    with TestClient(app) as client:
        db_manager.create_job("detail-test-job", "project_render", {"preview": True, "output_profile": "delivery"})
        db_manager.update_job_status("detail-test-job", "completed", 100, result_data={
            "output_path": str(ASSETS / "neutral_sample.mp4"),
            "quality_report": {"passed": True, "output_readable": True},
            "project_quality_report": {"final_decision": "NEEDS_REVIEW", "review_items": [{"category": "skin_safety"}]},
        })
        detail = client.get("/v1/jobs/detail-test-job/detail")
        assert detail.status_code == 200
        assert detail.json()["quality_report"]["passed"] is True
        assert detail.json()["review_category_counts"] == {"skin_safety": 1}

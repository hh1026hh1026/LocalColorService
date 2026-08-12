import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.comparison_media import build_comparison_inventory, resolve_job_source


def _write(path: Path, payload: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_inventory_groups_source_preview_and_render(tmp_path):
    data_dir = tmp_path / "data"
    source = _write(data_dir / "uploads" / "source.mp4")
    job_dir = data_dir / "jobs" / "abc123"
    _write(job_dir / "preview.mp4")
    _write(job_dir / "graded_video.mp4")
    (job_dir / "request.json").write_text(
        json.dumps({"input_path": str(source)}), encoding="utf-8"
    )
    (job_dir / "grade_recipe.json").write_text(
        json.dumps({"exposure": 0.25, "contrast": 1.1, "saturation": 0.95}),
        encoding="utf-8",
    )

    items = build_comparison_inventory(data_dir)

    assert {item["kind"] for item in items} == {"source", "preview", "render"}
    assert len({item["group_key"] for item in items}) == 1
    result = next(item for item in items if item["kind"] == "render")
    assert result["recipe_summary"] == "EV +0.25 · 对比 1.10 · 饱和 0.95"


def test_external_source_is_resolved_only_via_job_record(tmp_path):
    data_dir = tmp_path / "data"
    source = _write(tmp_path / "external" / "source.mp4")
    job_dir = data_dir / "jobs" / "abcdef12"
    _write(job_dir / "preview.mp4")
    (job_dir / "request.json").write_text(
        json.dumps({"input_path": str(source)}), encoding="utf-8"
    )

    items = build_comparison_inventory(data_dir)
    original = next(item for item in items if item["kind"] == "source")
    assert original["url"] == "/v1/comparison/jobs/abcdef12/source"
    assert resolve_job_source(data_dir, "abcdef12") == source.resolve()
    assert resolve_job_source(data_dir, "../external") is None


def test_comparison_api_returns_typed_media_inventory():
    with TestClient(app) as client:
        response = client.get("/v1/comparison/media")
        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload["items"], list)
        if payload["items"]:
            assert payload["items"][0]["url"].startswith(("/data/view/", "/v1/comparison/"))

from fastapi.testclient import TestClient

from app.main import app


def test_v02_discovery_endpoints():
    with TestClient(app) as client:
        assert client.get("/version").json()["version"] == "0.6.9"
        looks = client.get("/v1/looks").json()["looks"]
        assert len(looks) == 9
        assert all(item["must_protect"] for item in looks)
        models = client.get("/v1/models/status").json()["models"]
        assert "adaint" in models
        schema = client.get("/openapi.json").json()
        for path in (
            "/v1/color/scenes", "/v1/color/project-recipe", "/v1/color/project-render",
            "/v1/color/export", "/v1/color/adaint-lut", "/v1/color/reference-candidates",
            "/v1/color/reference-select", "/v1/color/lut-render",
            "/v1/color/grade-plan/revise", "/v1/color/grade-plan/approve",
            "/v1/color/scene-group-adaint",
            "/v1/color/timeline-preview", "/v1/color/timeline-adjust",
            "/v1/color/qc-auto-repair",
            "/v1/color/reference-preflight", "/v1/performance/status",
        ):
            assert path in schema["paths"]

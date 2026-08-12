from pathlib import Path

from app.schemas.api_schemas import (
    BatchQCRepairRequest, ReferencePreflightRequest, TimelineAdjustRequest, TimelinePreviewRequest,
)
from color_core.scene_analysis import detect_and_analyze_scenes


ASSETS = Path(__file__).parents[1] / "test_assets"


def test_scene_analysis_produces_cached_proxy_and_timeline_thumbnail(tmp_path):
    result = detect_and_analyze_scenes(
        str(ASSETS / "neutral_sample.mp4"), artifact_dir=str(tmp_path),
    )
    assert result.acceleration["proxy_used"] is True
    assert Path(result.analysis_source_path).is_file()
    assert result.scenes[0].thumbnail_path == "thumbnails/scene_0001.jpg"
    assert (tmp_path / result.scenes[0].thumbnail_path).is_file()


def test_timeline_and_reference_requests_enforce_explicit_targets():
    preview = TimelinePreviewRequest(project_job_id="project", scope="shot", shot_id="scene_0001")
    assert preview.context_seconds == 1.0
    adjust = TimelineAdjustRequest(
        project_job_id="project", expected_revision=1, scope="scene_group",
        scene_group_id="group_0001", operation="match_hero",
    )
    assert adjust.operation == "match_hero"
    reference = ReferencePreflightRequest(reference_path=r"F:\reference.png", scene_group_id="__all__")
    assert reference.scene_group_id == "__all__"


def test_timeline_auto_repair_requires_a_qc_category():
    repair = TimelineAdjustRequest(
        project_job_id="project", expected_revision=1, scope="shot",
        shot_id="shot_0001", operation="auto_repair", repair_category="skin_safety",
    )
    assert repair.repair_category == "skin_safety"


def test_batch_qc_repair_request_accepts_multiple_categories():
    request = BatchQCRepairRequest(
        project_job_id="project", expected_revision=2,
        repairs=[
            {"shot_id": "shot_0001", "category": "large_exposure_correction"},
            {"shot_id": "shot_0002", "category": "skin_safety"},
        ],
    )
    assert len(request.repairs) == 2
    assert request.repairs[1].category == "skin_safety"

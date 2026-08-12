import json
from pathlib import Path

import numpy as np

from color_core.artifact_freeze import freeze_approved_revision, verify_approval_bundle
from color_core.grade_decision import decide_grade
from color_core.image_analyzer import analyze_image_frames
from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, ShotGrade, TransformAsset
from color_core.recipe import GradeRecipe
from color_core.scene_analysis import classify_special_content


def test_near_black_content_is_preserved():
    frames = [np.zeros((72, 128, 3), dtype=np.uint8) for _ in range(3)]
    report = analyze_image_frames(frames)
    content_class, flags = classify_special_content(frames, report)
    decision = decide_grade(report, content_flags=flags)
    assert content_class == "black_or_near_black"
    assert flags["preserve_intent"]
    assert decision.action == "preserve"
    assert decision.recommended_look_strength == 0.0


def test_approved_revision_freezes_and_verifies_assets(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test-media")
    lut = tmp_path / "look.cube"
    lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8, encoding="utf-8")
    project = ProjectGradeRecipe(
        source_path=str(source),
        shots=[ShotGrade(
            scene_id="scene_0001", shot_id="shot_0001", start_time=0.0, end_time=1.0,
            base_correction=GradeRecipe(),
        )],
        scene_groups=[SceneGroup(
            scene_group_id="group_0001", shot_ids=["shot_0001"], hero_shot_id="shot_0001",
            creative_transform=TransformAsset(provider="test", provider_version="1", asset_path=str(lut)),
            approved_candidate_id="A",
        )],
    )
    project.approve("tester")
    bundle = tmp_path / "approved"
    manifest = freeze_approved_revision(project, bundle)
    assert manifest["assets"]
    assert project.source_media_hash
    assert project.scene_groups[0].creative_transform.sha256
    assert Path(project.resolve_transform_path(project.scene_groups[0].creative_transform)).is_file()
    assert verify_approval_bundle(bundle)["valid"]

    frozen_lut = bundle / project.scene_groups[0].creative_transform.relative_path
    frozen_lut.write_text("tampered", encoding="utf-8")
    verification = verify_approval_bundle(bundle)
    assert not verification["valid"]
    assert any("hash mismatch" in item for item in verification["errors"])


def test_legacy_project_without_new_fields_remains_readable():
    legacy = {
        "recipe_version": "0.3.0",
        "source_path": "legacy.mp4",
        "shots": [{
            "scene_id": "scene_0001", "start_time": 0.0, "end_time": 1.0,
            "base_correction": GradeRecipe().model_dump(mode="json"),
        }],
        "scene_groups": [{
            "scene_group_id": "group_0001", "shot_ids": ["scene_0001"], "hero_shot_id": "scene_0001",
        }],
    }
    restored = ProjectGradeRecipe.model_validate(json.loads(json.dumps(legacy)))
    assert restored.shots[0].shot_id == "scene_0001"
    assert restored.scene_groups[0].creative_source == "project_look_inherited"

from pathlib import Path

from color_core.project_recipe import ProjectGradeRecipe, TransformAsset, project_from_scene_analysis
from color_core.scene_analysis import detect_and_analyze_scenes
from color_core.scene_grouping import suggest_scene_groups


ASSETS = Path(__file__).parents[1] / "test_assets"


def test_all_video_reference_scope_can_attach_one_transform_to_every_group(tmp_path):
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    analysis = detect_and_analyze_scenes(source)
    project = project_from_scene_analysis(source, analysis, "neutral_broadcast")
    project.scene_groups = suggest_scene_groups(analysis)
    asset = TransformAsset(provider="canoncgt", asset_path=str(tmp_path / "look.cube"))
    for group in project.scene_groups:
        group.creative_transform = asset.model_copy(deep=True)
    restored = ProjectGradeRecipe.model_validate(project.model_dump())
    assert all(group.creative_transform.provider == "canoncgt" for group in restored.scene_groups)
    assert {shot.shot_id for shot in restored.shots} == {
        shot_id for group in restored.scene_groups for shot_id in group.shot_ids
    }

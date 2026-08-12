from pathlib import Path

import pytest

from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, project_from_scene_analysis
from color_core.scene_analysis import detect_and_analyze_scenes
from color_core.scene_grouping import suggest_scene_groups


ASSETS = Path(__file__).parents[1] / "test_assets"


def test_scene_groups_cover_shots_and_grade_plan_is_versioned():
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    analysis = detect_and_analyze_scenes(source)
    groups = suggest_scene_groups(analysis)
    # V2 groups by lighting coherence, not adjacency, so a group may interleave
    # with another in time (shot/reverse-shot). The invariant is coverage:
    # every shot appears exactly once, not that groups are contiguous.
    assigned = [shot for group in groups for shot in group.shot_ids]
    expected = [f"shot_{item.index + 1:04d}" for item in analysis.scenes]
    assert sorted(assigned) == sorted(expected)
    assert len(assigned) == len(set(assigned))
    project = project_from_scene_analysis(source, analysis, "neutral_broadcast", "scene-job")
    project.scene_groups = groups
    restored = ProjectGradeRecipe.model_validate(project.model_dump())
    assert restored.recipe_version == "0.4.0"
    assert restored.revision == 1
    assert restored.status == "draft"


def test_grade_plan_revision_approval_and_group_validation():
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    analysis = detect_and_analyze_scenes(source)
    project = project_from_scene_analysis(source, analysis, "neutral_broadcast")
    project.revise("manual_revision", details={"field": "look_strength"})
    assert project.revision == 2 and project.status == "draft"
    project.approve("tester")
    assert project.revision == 3 and project.status == "approved" and project.approved_by == "tester"
    with pytest.raises(ValueError, match="hero shot"):
        SceneGroup(scene_group_id="bad", shot_ids=["shot_1"], hero_shot_id="shot_2")

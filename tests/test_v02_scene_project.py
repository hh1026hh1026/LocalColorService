from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET

from color_core.interchange import export_cc, export_ccc, export_clf
from color_core.look_packages import apply_look_package, list_look_packages
from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.project_qc import evaluate_project_quality, inspect_cube_lut
from color_core.project_recipe import ProjectGradeRecipe, project_from_scene_analysis
from color_core.recipe import GradeRecipe
from color_core.scene_analysis import detect_and_analyze_scenes
from color_core.timeline_renderer import build_timeline_render_command, render_shot_timeline


ASSETS = Path(__file__).parents[1] / "test_assets"


def test_scene_detection_and_layered_project_recipe():
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    result = detect_and_analyze_scenes(source)
    assert result.scenes
    assert result.scenes[0].representative_times
    assert result.scenes[0].analysis.luminance.p01 <= result.scenes[0].analysis.luminance.p99
    project = project_from_scene_analysis(source, result, "cinematic_soft_print", "deadbeef")
    restored = ProjectGradeRecipe.model_validate(project.model_dump())
    effective = restored.effective_recipe(restored.shots[0])
    assert effective.look_id == "cinematic_soft_print"
    assert effective.highlight_softness > 0


def test_quality_profile_adds_auditable_safety_policy():
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    result = detect_and_analyze_scenes(source)
    project = project_from_scene_analysis(source, result, "neutral_broadcast")
    project.quality_profile = "broadcast_safe"
    effective = project.effective_recipe(project.shots[0])
    assert effective.skin_protection >= 0.95
    assert effective.gamut_protection >= 0.98
    assert effective.parameter_sources["skin_protection"] == "quality_profile"
    assert effective.parameter_confidence["skin_protection"] >= 0.90


def test_look_packages_and_interchange_exports(tmp_path):
    manager = OCIOManager()
    looks = list_look_packages()
    assert {item["id"] for item in looks} == {
        "neutral_broadcast", "clean_commercial", "cinematic_soft_print",
        "restrained_teal_amber", "warm_memory", "cold_thriller",
        "sports_vivid", "stage_mixed_light", "day_for_night",
    }
    assert all(item["suitable_for"] and item["must_protect"] and item["design_basis"] for item in looks)
    recipe = manager.complete_recipe(apply_look_package(GradeRecipe(), "clean_commercial", 0.6))
    cc = export_cc(recipe, str(tmp_path / "grade.cc"))
    clf = export_clf(recipe, str(tmp_path / "grade.clf"), manager)
    assert ET.parse(cc).getroot().tag == "ColorCorrection"
    assert "ProcessList" in Path(clf).read_text(encoding="utf-8")


def test_all_look_packages_produce_valid_distinct_recipes(tmp_path):
    manager = OCIOManager()
    recipes = {
        item["id"]: apply_look_package(GradeRecipe(), item["id"])
        for item in list_look_packages()
    }
    assert len({(recipe.exposure, recipe.contrast, recipe.saturation, recipe.temperature,
                 tuple(recipe.shadow_color)) for recipe in recipes.values()}) == len(recipes)
    assert recipes["day_for_night"].exposure <= -1.0
    assert recipes["sports_vivid"].gamut_protection == 1.0
    assert recipes["stage_mixed_light"].skin_protection == 1.0
    assert recipes["restrained_teal_amber"].shadow_color[2] > recipes["restrained_teal_amber"].shadow_color[0]
    protected_group = manager.build_grade_group(recipes["sports_vivid"])
    assert type(protected_group[0]).__name__ == "LookTransform"
    for look_id, recipe in recipes.items():
        lut = bake_3d_lut(recipe, str(tmp_path / f"{look_id}.cube"), manager)
        assert inspect_cube_lut(lut).passed


def test_look_blend_clamps_qc_protection_values():
    repaired = apply_look_package(GradeRecipe(skin_protection=0.95), "clean_commercial")
    assert 0.0 <= repaired.skin_protection <= 1.0


def test_project_ccc_lut_qc_and_timeline_render(tmp_path):
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    scenes = detect_and_analyze_scenes(source)
    project = project_from_scene_analysis(source, scenes, "neutral_broadcast")
    manager = OCIOManager()
    recipe = manager.complete_recipe(project.effective_recipe(project.shots[0]))
    lut = bake_3d_lut(recipe, str(tmp_path / "shot.cube"), manager)
    lut_report = inspect_cube_lut(lut)
    assert lut_report.passed
    ccc = export_ccc(project, str(tmp_path / "project.ccc"))
    assert ET.parse(ccc).getroot().tag == "ColorCorrectionCollection"
    command = build_timeline_render_command(source, [(0.0, 3.0, lut)], str(tmp_path / "out.mp4"), "libx264")
    assert "tetrahedral" in " ".join(command)
    output = render_shot_timeline(source, [(0.0, 3.0, lut)], str(tmp_path / "out.mp4"), 360, True)
    assert Path(output).stat().st_size > 0
    project_qc = evaluate_project_quality(project, scenes, [lut])
    assert project_qc.passed


def test_large_timeline_uses_filter_script_to_avoid_windows_command_limit(tmp_path):
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    lut = str((Path(__file__).parents[1] / "data" / "manual_verify" / "v02_test.cube").resolve())
    shots = [(index * 0.1, (index + 1) * 0.1, lut) for index in range(179)]
    script = tmp_path / "timeline.ffgraph"
    command = build_timeline_render_command(
        source, shots, str(tmp_path / "out.mp4"), "libx264", filter_script_path=str(script),
    )
    assert "-filter_complex_script" in command
    assert "-filter_complex" not in command
    assert script.is_file() and "concat=n=179" in script.read_text(encoding="utf-8")
    assert len(subprocess.list2cmdline(command)) < 4096


def test_partial_timeline_trims_and_concatenates_audio(tmp_path):
    source = str((ASSETS / "neutral_sample.mp4").resolve())
    lut = str((Path(__file__).parents[1] / "data" / "manual_verify" / "v02_test.cube").resolve())
    script = tmp_path / "partial.ffgraph"
    command = build_timeline_render_command(
        source, [(0.0, 0.8, lut), (1.6, 2.5, lut)], str(tmp_path / "partial.mp4"),
        "libx264", "aac", 360, str(script), trim_audio=True,
    )
    graph = script.read_text(encoding="utf-8")
    assert "atrim=start=0.000000:end=0.800000" in graph
    assert "concat=n=2:v=0:a=1[aout]" in graph
    assert command[command.index("-map") + 1] == "[vout]"
    assert "[aout]" in command

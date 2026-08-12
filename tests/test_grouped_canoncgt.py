from pathlib import Path

import numpy as np

import workers.job_worker as worker_module
from color_core.cube_tools import write_cube
from color_core.look_provider import CandidateScore, LookCandidate
from color_core.project_recipe import ProjectGradeRecipe, SceneGroup, ShotGrade
from color_core.recipe import GradeRecipe
from workers.job_worker import JobWorker


ROOT = Path(__file__).parents[1]


def _identity(path: Path) -> str:
    size = 33
    grid = np.linspace(0.0, 1.0, size)
    blue, green, red = np.meshgrid(grid, grid, grid, indexing="ij")
    return write_cube(np.stack((red, green, blue), axis=-1), str(path), "identity")


def test_all_scope_generates_and_selects_distinct_group_luts(tmp_path, monkeypatch):
    source = str((ROOT / "test_assets" / "neutral_sample.mp4").resolve())
    reference = str((ROOT / "test_assets" / "overexposed_sample.mp4").resolve())
    project = ProjectGradeRecipe(
        source_path=source,
        shots=[
            ShotGrade(scene_id="scene_0001", start_time=0.0, end_time=1.5, base_correction=GradeRecipe()),
            ShotGrade(scene_id="scene_0002", start_time=1.5, end_time=3.0, base_correction=GradeRecipe()),
        ],
        scene_groups=[
            SceneGroup(scene_group_id="group_0001", shot_ids=["scene_0001"], hero_shot_id="scene_0001"),
            SceneGroup(scene_group_id="group_0002", shot_ids=["scene_0002"], hero_shot_id="scene_0002"),
        ],
    )
    monkeypatch.setattr(worker_module, "job_directory", lambda job_id: tmp_path / job_id)
    monkeypatch.setattr(worker_module, "write_json", lambda path, data: None)
    monkeypatch.setattr(worker_module, "apply_cube_to_bgr", lambda frame, path: frame)
    monkeypatch.setattr(
        worker_module, "bake_3d_lut",
        lambda recipe, path, manager: _identity(Path(path)),
    )
    import color_core.frame_sampler as sampler
    monkeypatch.setattr(sampler, "sample_frames_at", lambda path, times, target_height=0: [np.full((64, 96, 3), 112, np.uint8) for _ in times])

    class FakeCanon:
        calls = 0

        def __init__(self, manager):
            pass

        def generate_candidates(self, context):
            FakeCanon.calls += 1
            directory = Path(context["output_dir"])
            directory.mkdir(parents=True, exist_ok=True)
            items = []
            for candidate_id, strength in (("A", 0.45), ("B", 0.65), ("C", 0.85)):
                path = _identity(directory / f"{candidate_id}.cube")
                items.append(LookCandidate(
                    id=candidate_id, label=candidate_id, provider="canoncgt", lut_path=path,
                    strength=strength,
                    score=CandidateScore(technical_safety=1.0, continuity=0.9, reference_match=0.8, total=0.87),
                    metadata={"fit_rmse": 0.01 * FakeCanon.calls},
                ))
            return items

    monkeypatch.setattr(worker_module, "CanonCGTProvider", FakeCanon)
    worker = JobWorker()
    monkeypatch.setattr(worker, "_project_from_job", lambda job_id, label="GradePlan": project.model_copy(deep=True))
    result = worker._handle_reference_candidates("candidate", {
        "source_path": source, "reference_path": reference, "engine": "canoncgt",
        "project_job_id": "project", "scene_group_id": "__all__", "allow_fallback": False,
    })
    assert FakeCanon.calls == 2
    group_luts = result["candidates"][1]["metadata"]["group_luts"]
    assert set(group_luts) == {"group_0001", "group_0002"}
    assert group_luts["group_0001"] != group_luts["group_0002"]

    monkeypatch.setattr(worker, "_completed_job", lambda job_id, label: {"result_data": result})
    (tmp_path / "selection").mkdir()
    selection = worker._handle_candidate_select("selection", {
        "candidate_job_id": "candidate", "candidate_id": "B", "project_job_id": "project",
        "scene_group_id": "__all__", "expected_revision": project.revision,
    })
    selected_project = ProjectGradeRecipe.model_validate(selection["project_recipe"])
    assets = [group.creative_transform.asset_path for group in selected_project.scene_groups]
    assert len(set(assets)) == 2
    assert all(Path(path).is_file() for path in assets)

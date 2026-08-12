"""End-to-end smoke test: the flow an operator actually runs.

Every other test exercises one stage. This one runs the whole chain against a
synthetic multi-scene clip - shot detection, analysis, grade plan, scene groups,
white-balance harmonisation, LUT baking, timeline render, QC - and asserts the
things that would make a real session fail.

It exists because several defects reached production despite full unit coverage:
the chained-LUT change broke face-selective rendering (a type mismatch between
two stages), and a colour-metadata gap in the analysis proxy was invisible to
every component test. Both were interface problems, which only a full run shows.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    """Four scenes with different light, plus a fade to black at the end.

    Deliberately awkward: a warm interior, a cool exterior, a dark scene that
    needs lifting and a bright one that has no headroom - the four cases the
    automatic pass has to tell apart.
    """
    root = tmp_path_factory.mktemp("smoke")
    segments = []
    for index, (colour, seconds) in enumerate([
        ("0xB08050", 2.0),   # warm interior
        ("0x6080C0", 2.0),   # cool exterior
        ("0x201810", 2.0),   # dark, wants a lift
        ("0xE8E0D0", 2.0),   # bright, no headroom
    ]):
        path = root / f"seg{index}.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"color=c={colour}:s=640x360:r=25:d={seconds}",
             "-f", "lavfi", "-i", f"nullsrc=s=640x360:r=25:d={seconds}",
             "-filter_complex",
             "[0:v]noise=alls=18:allf=t+u,drawbox=x=220:y=90:w=200:h=180:color=0xC08870@1:t=fill[v]",
             "-map", "[v]", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(path)],
            check=True, capture_output=True,
        )
        segments.append(path)
    listing = root / "list.txt"
    listing.write_text("".join(f"file '{item}'\n" for item in segments), encoding="utf-8")
    output = root / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(output)],
        check=True, capture_output=True,
    )
    return str(output)


@pytest.fixture(scope="module")
def worker(tmp_path_factory, monkeypatch_module=None):
    from workers.job_worker import JobWorker

    return JobWorker(lane="heavy")


def _run(worker, handler: str, job_id: str, params: dict) -> dict:
    """Run one stage the way the worker does, including the database record.

    Stages read their upstream input back out of the job table, so a smoke test
    that called the handlers directly without recording them would not exercise
    the same code path an operator does.
    """
    from app.services.database import db_manager
    from app.services.job_store import artifact_map, job_directory, write_json

    job_directory(job_id).mkdir(parents=True, exist_ok=True)
    write_json(job_directory(job_id) / "request.json", {"job_id": job_id, **params})
    if not db_manager.get_job(job_id):
        db_manager.create_job(job_id, handler, params)
    result = getattr(worker, f"_handle_{handler}")(job_id, params)
    result["artifacts"] = artifact_map(job_id)
    db_manager.update_job_status(job_id, "completed", 100, result_data=result)
    return result


def test_full_pipeline_runs_and_produces_a_graded_file(clip, worker):
    scenes = _run(worker, "scenes", "5c0e00000001", {"file_path": clip})
    assert scenes["scene_count"] >= 2, "shot detection found nothing to work with"

    recipe = _run(worker, "project_recipe", "5c0e00000002", {
        "scene_job_id": "5c0e00000001", "look_id": "neutral_broadcast",
    })
    assert recipe["shot_count"] == scenes["scene_count"]
    assert recipe["scene_group_count"] >= 1
    # Harmonisation must have run and reported per group.
    assert len(recipe["white_balance_diagnostics"]) == recipe["scene_group_count"]

    render = _run(worker, "project_render", "5c0e00000003", {
        "project_job_id": "5c0e00000002", "input_path": clip, "allow_unapproved": True,
    })
    output = Path(render["output_path"])
    assert output.is_file() and output.stat().st_size > 0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=width,height,nb_frames,pix_fmt",
         "-of", "default=nw=1:nk=0", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "pix_fmt=yuv420p" in probe


def test_the_render_carries_correct_colour_metadata(clip, worker):
    output = Path(_run(worker, "project_render", "5c0e00000004", {
        "project_job_id": "5c0e00000002", "input_path": clip, "allow_unapproved": True,
    })["output_path"])
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=color_space,color_primaries,color_transfer,color_range",
         "-of", "default=nw=1:nk=0", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout
    for expected in ("color_space=bt709", "color_primaries=bt709", "color_transfer=bt709"):
        assert expected in tags, f"missing {expected} in:\n{tags}"


def test_quality_report_is_complete_and_self_consistent(worker):
    from app.services.job_store import job_directory

    report = json.loads(
        (job_directory("5c0e00000003") / "project_quality_report.json").read_text(encoding="utf-8")
    )
    for field in ("final_decision", "base_correction", "creative_transform",
                  "scene_continuity", "within_group_continuity", "skin_safety",
                  "lut_warning_summary", "large_exposure_corrections"):
        assert field in report, f"QC report is missing {field}"
    severity = {"PASS": 0, "PASS_WITH_WARNINGS": 1, "NEEDS_REVIEW": 2, "FAIL": 3}
    components = [report[key] for key in
                  ("render_integrity", "base_correction", "creative_transform",
                   "scene_continuity", "skin_safety")]
    assert severity[report["final_decision"]] == max(severity[item] for item in components)
    # Every LUT was inspected under an explicit role.
    assert report["lut_warning_summary"], "no LUTs were inspected"
    assert all(role in ("technical", "creative", "combined", "output")
               for role in report["lut_warning_summary"])


def test_recipe_records_the_diagnostics_a_reviewer_needs(worker):
    from app.services.job_store import job_directory

    recipe = json.loads(
        (job_directory("5c0e00000002") / "project_recipe.json").read_text(encoding="utf-8")
    )
    for shot in recipe["shots"]:
        base = shot["base_correction"]
        assert base["recipe_version"] == "0.6.0"
        assert base["white_balance"], "white balance provenance missing"
        assert base["white_balance"]["method"] == "cat_bradford_linear"
        assert "exposure_diagnostics" in base
        assert base["exposure_diagnostics"].get("anchor"), "exposure anchor not recorded"


def test_dark_and_bright_scenes_are_treated_differently(worker):
    """The pipeline must not apply the same exposure everywhere."""
    from app.services.job_store import job_directory

    recipe = json.loads(
        (job_directory("5c0e00000002") / "project_recipe.json").read_text(encoding="utf-8")
    )
    exposures = [shot["base_correction"]["exposure"] for shot in recipe["shots"]]
    headrooms = [
        shot["base_correction"]["exposure_diagnostics"].get("headroom_ev", 0.0)
        for shot in recipe["shots"]
    ]
    assert np.ptp(exposures) > 0.05 or np.ptp(headrooms) > 0.05, (
        f"every shot got the same treatment: exposures={exposures}"
    )
    # The bright scene must have less headroom than the dark one.
    assert min(headrooms) < max(headrooms)


def test_no_shot_exceeds_the_safety_envelope(worker):
    from app.services.job_store import job_directory

    recipe = json.loads(
        (job_directory("5c0e00000002") / "project_recipe.json").read_text(encoding="utf-8")
    )
    from color_core.white_balance import (
        MAX_CORRECTION_MIRED, cct_duv_from_white, source_white_from_gains,
    )

    for shot in recipe["shots"]:
        base = shot["base_correction"]
        assert abs(base["exposure"]) <= 0.8 + 1e-6
        assert 0.85 <= base["contrast"] <= 1.20
        assert 0.85 <= base["saturation"] <= 1.15
        # White balance is bounded by how far it moves the white point, not by
        # a fixed gain range - a per-channel clamp saturates and manufactures
        # discontinuities between shots either side of it.
        analysis_cct = shot["base_correction"]["white_balance"].get("source_cct_kelvin", 0)
        if analysis_cct:
            applied_cct, _ = cct_duv_from_white(source_white_from_gains(tuple(base["rgb_gains"])))
            shift = abs(1e6 / max(applied_cct, 1.0) - 1e6 / max(analysis_cct, 1.0))
            assert shift <= MAX_CORRECTION_MIRED + 2.0, (
                f"{shot['shot_id']} moved the white point by {shift:.1f} mired"
            )

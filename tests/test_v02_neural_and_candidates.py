from pathlib import Path

from color_core.frame_sampler import sample_frames_at
from color_core.neural_models import generate_adaint_lut, neural_runtime_status
from color_core.ocio_manager import OCIOManager
from color_core.project_qc import inspect_cube_lut
from color_core.reference_candidates import generate_reference_candidates


ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "test_assets"


def test_official_adaint_checkpoint_generates_valid_uniform_lut(tmp_path):
    status = neural_runtime_status()["adaint"]
    assert status["available"], status
    frame = sample_frames_at(str(ASSETS / "neutral_sample.mp4"), [1.5])[0]
    result = generate_adaint_lut(frame, str(tmp_path / "adaint.cube"))
    assert result["model"] == "AdaInt AiLUT-FiveK-sRGB"
    assert inspect_cube_lut(result["lut_path"]).passed


def test_reference_image_builds_three_selectable_candidates(tmp_path):
    source = sample_frames_at(str(ASSETS / "neutral_sample.mp4"), [1.5])[0]
    reference = sample_frames_at(str(ASSETS / "overexposed_sample.mp4"), [1.5])[0]
    candidates = generate_reference_candidates(source, reference, str(tmp_path), OCIOManager())
    assert [item["id"] for item in candidates] == ["A", "B", "C"]
    assert all(inspect_cube_lut(item["lut_path"]).passed for item in candidates)

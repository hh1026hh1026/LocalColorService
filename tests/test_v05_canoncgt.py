from pathlib import Path

from color_core.canoncgt_provider import CanonCGTProvider, canoncgt_runtime_status, inspect_reference_frame
from color_core.frame_sampler import sample_frames_at
from color_core.ocio_manager import OCIOManager
from color_core.project_qc import inspect_cube_lut


ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "test_assets"


def test_canoncgt_official_runtime_and_fitted_candidates(tmp_path):
    status = canoncgt_runtime_status()
    assert status["available"], status
    source = sample_frames_at(str(ASSETS / "neutral_sample.mp4"), [1.5])[0]
    reference = sample_frames_at(str(ASSETS / "overexposed_sample.mp4"), [1.5])[0]
    assert inspect_reference_frame(reference)["passed"]
    candidates = CanonCGTProvider(OCIOManager()).generate_candidates({
        "source_frames": [source], "reference_frame": reference,
        "output_dir": str(tmp_path), "lut_size": 33, "allow_fallback": False,
    })
    assert [item.provider for item in candidates] == ["statistical", "canoncgt", "canoncgt"]
    assert not any(item.fallback_used for item in candidates)
    assert all(item.passed and inspect_cube_lut(item.lut_path).passed for item in candidates)
    assert candidates[1].metadata["fit_rmse"] >= 0.0


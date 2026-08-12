from pathlib import Path

import numpy as np

from color_core.frame_sampler import ensure_analysis_proxy, sample_frames, sample_frames_at, uniform_sample_timestamps


ASSETS = Path(__file__).parents[1] / "test_assets"


def test_uniform_timestamps_and_extraction():
    timestamps = uniform_sample_timestamps(10.0, 5)
    assert len(timestamps) == 5
    assert np.allclose(np.diff(timestamps), np.diff(timestamps)[0])
    frames = sample_frames(str(ASSETS / "neutral_sample.mp4"), 3, 3.0)
    assert len(frames) == 3
    assert all(frame.ndim == 3 for frame in frames)


def test_batch_explicit_frames_and_gpu_proxy_cache():
    source = str(ASSETS / "neutral_sample.mp4")
    proxy, acceleration = ensure_analysis_proxy(source, 360)
    assert Path(proxy).is_file()
    assert acceleration["proxy_used"] is True
    frames = sample_frames_at(proxy, [0.3, 1.5, 2.7])
    assert len(frames) == 3
    assert all(frame.shape[0] == 360 for frame in frames)

"""Fit a stable global 3D LUT from aligned source and graded target frames."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter
from color_core.face_analysis import skin_mask_bgr


def _sample_lut(values: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Vectorized trilinear sampling for a LUT stored on B,G,R axes."""
    size = values.shape[0]
    position = np.clip(rgb[:, [2, 1, 0]], 0.0, 1.0) * (size - 1)
    low = np.floor(position).astype(np.int32)
    high = np.minimum(low + 1, size - 1)
    fraction = position - low
    result = np.zeros((len(rgb), 3), dtype=np.float64)
    for db in (0, 1):
        bi = high[:, 0] if db else low[:, 0]
        wb = fraction[:, 0] if db else 1.0 - fraction[:, 0]
        for dg in (0, 1):
            gi = high[:, 1] if dg else low[:, 1]
            wg = fraction[:, 1] if dg else 1.0 - fraction[:, 1]
            for dr in (0, 1):
                ri = high[:, 2] if dr else low[:, 2]
                wr = fraction[:, 2] if dr else 1.0 - fraction[:, 2]
                result += values[bi, gi, ri] * (wb * wg * wr)[:, None]
    return result


def _features(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    return np.column_stack((
        np.ones(len(rgb)), r, g, b, r * r, g * g, b * b,
        r * g, r * b, g * b, r * g * b,
    ))


def _write_cube(values: np.ndarray, output_path: str, title: str) -> str:
    size = values.shape[0]
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'TITLE "{title}"', f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    lines.extend("{:.8f} {:.8f} {:.8f}".format(*rgb) for rgb in values.reshape(-1, 3))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def fit_lut_from_pairs(
    source_frames: list[np.ndarray],
    target_frames: list[np.ndarray],
    output_path: str,
    size: int = 33,
    strength: float = 1.0,
    max_samples_per_frame: int = 50000,
) -> dict:
    """Fit a regularized polynomial mapping, sampled as a smooth uniform LUT.

    Source and target frames must be spatially aligned. Neutral grayscale and
    cube-boundary anchors pull unseen colors toward identity, avoiding unstable
    extrapolation from one hero frame.
    """
    if len(source_frames) != len(target_frames) or not source_frames:
        raise ValueError("source and target frame lists must be non-empty and aligned")
    source_samples: list[np.ndarray] = []
    target_samples: list[np.ndarray] = []
    validation_source: list[np.ndarray] = []
    validation_target: list[np.ndarray] = []
    protected_skin: list[np.ndarray] = []
    for index, (source, target) in enumerate(zip(source_frames, target_frames)):
        if source.shape[:2] != target.shape[:2]:
            target = cv2.resize(target, (source.shape[1], source.shape[0]), interpolation=cv2.INTER_AREA)
        source_rgb = cv2.cvtColor(source, cv2.COLOR_BGR2RGB).reshape(-1, 3).astype(np.float64) / 255.0
        target_rgb = cv2.cvtColor(target, cv2.COLOR_BGR2RGB).reshape(-1, 3).astype(np.float64) / 255.0
        count = min(max_samples_per_frame, len(source_rgb))
        rng = np.random.default_rng(1701 + index)
        selected = rng.choice(len(source_rgb), count, replace=False)
        validation_count = max(256, int(count * 0.20)) if count >= 512 else max(1, count // 5)
        validation = selected[:validation_count]
        training = selected[validation_count:]
        source_samples.append(source_rgb[training])
        target_samples.append(target_rgb[training])
        validation_source.append(source_rgb[validation])
        validation_target.append(target_rgb[validation])
        skin_mask, boxes = skin_mask_bgr(source)
        skin_indexes = np.flatnonzero(skin_mask.reshape(-1) >= 64)
        if boxes and len(skin_indexes):
            protected_count = min(6000, len(skin_indexes))
            protected_skin.append(source_rgb[rng.choice(skin_indexes, protected_count, replace=False)])
    source_data = np.concatenate(source_samples)
    target_data = np.concatenate(target_samples)

    axis = np.linspace(0.0, 1.0, 9)
    neutral = np.column_stack((axis, axis, axis))
    boundary = np.asarray([
        [r, g, b] for b in (0.0, 1.0) for g in (0.0, 1.0) for r in (0.0, 1.0)
    ], dtype=np.float64)
    anchors = np.repeat(np.concatenate((neutral, boundary)), 250, axis=0)
    skin_anchors = np.concatenate(protected_skin) if protected_skin else np.empty((0, 3), dtype=np.float64)
    # A modest identity prior on detected skin keeps reference matching from
    # rotating skin hue aggressively while still allowing exposure and density.
    skin_anchors = np.repeat(skin_anchors, 2, axis=0)
    x = np.concatenate((source_data, anchors, skin_anchors))
    y = np.concatenate((target_data, anchors, skin_anchors))
    matrix = _features(x)
    regularization = np.eye(matrix.shape[1]) * 2e-3
    regularization[0, 0] = 1e-5
    coefficients = np.linalg.solve(matrix.T @ matrix + regularization, matrix.T @ y)

    grid = np.linspace(0.0, 1.0, size)
    blue, green, red = np.meshgrid(grid, grid, grid, indexing="ij")
    identity = np.stack((red, green, blue), axis=-1)
    predicted = (_features(identity.reshape(-1, 3)) @ coefficients).reshape(size, size, size, 3)
    predicted = gaussian_filter(predicted, sigma=(0.45, 0.45, 0.45, 0.0), mode="nearest")
    amount = float(np.clip(strength, 0.0, 1.0))
    lut = np.clip(identity + (predicted - identity) * amount, 0.0, 1.0)
    lut = np.clip(lut, 0.0, 1.0)

    # Score the exported LUT, after smoothing and requested strength, on pixels
    # held out from fitting. This makes B/C scores meaningful and detects damage
    # introduced by LUT regularization itself.
    validation_source_data = np.concatenate(validation_source)
    validation_target_data = np.concatenate(validation_target)
    fitted_samples = _sample_lut(lut, validation_source_data)
    expected_target = validation_source_data + (validation_target_data - validation_source_data) * amount
    error = fitted_samples - expected_target
    rmse = float(np.sqrt(np.mean(error * error)))
    p95 = float(np.percentile(np.linalg.norm(error, axis=1), 95))
    raw_fitted = np.clip(_features(validation_source_data) @ coefficients, 0.0, 1.0)
    raw_rmse = float(np.sqrt(np.mean((raw_fitted - validation_target_data) ** 2)))
    path = _write_cube(lut, output_path, "LocalColorService CanonCGT fitted transform")
    return {
        "lut_path": path,
        "fit_rmse": round(rmse, 8),
        "fit_p95": round(p95, 8),
        "raw_model_rmse": round(raw_rmse, 8),
        "validation_sample_count": int(len(validation_source_data)),
        "sample_count": int(len(source_data)),
        "lut_size": size,
        "strength": amount,
    }

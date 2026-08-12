"""Lightweight face and skin analysis used by grading decisions and QC.

The implementation deliberately has no mandatory model download. OpenCV's
bundled frontal-face cascade supplies a conservative face region; a YCrCb skin
classifier refines that region. The result is suitable for protection and
quality gates, not identity recognition.
"""

from __future__ import annotations

from functools import lru_cache
from threading import RLock

import cv2
import numpy as np
from pydantic import BaseModel, Field


class FaceAnalysis(BaseModel):
    backend: str = "opencv_haar_ycrcb"
    face_count: int = 0
    face_area_ratio: float = 0.0
    skin_area_ratio: float = 0.0
    confidence: float = 0.0
    mean_skin_hue: float | None = None
    mean_skin_luminance: float | None = None
    # Median skin RGB in Rec.709-encoded [0,1]. Kept so QC can compute a real
    # CIEDE2000 between source and graded skin instead of comparing an HSV hue
    # scalar, which is unstable and underestimates shifts in the orange region.
    mean_skin_rgb: list[float] | None = None
    warnings: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def _cascade() -> cv2.CascadeClassifier:
    path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(path)
    if detector.empty():
        raise RuntimeError(f"OpenCV face detector is unavailable: {path}")
    return detector


def skin_mask_bgr(
    frame: np.ndarray,
    face_only: bool = True,
    reuse_boxes: list[tuple[int, int, int, int]] | None = None,
) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    """Return a soft uint8 skin mask and detected face boxes.

    ``reuse_boxes`` skips the Haar cascade and reuses previously detected face
    boxes. Video mask generation uses this to run detection on a subset of
    frames: a face does not move meaningfully between adjacent frames, and the
    caller's temporal smoothing absorbs the residual.
    """
    height, width = frame.shape[:2]
    if reuse_boxes is not None:
        boxes = list(reuse_boxes)
    else:
        scale = min(1.0, 720.0 / max(height, width))
        small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        # OpenCV's Haar classifier instance is cached globally.  Concurrent
        # calls can corrupt its internal state on Windows/OpenCV builds.
        with _CASCADE_LOCK:
            detected = _cascade().detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(28, 28))
        boxes = [
            (int(x / scale), int(y / scale), int(w / scale), int(h / scale))
            for x, y, w, h in detected
        ]

    ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    # Broad thresholds; the face ROI prevents similarly colored backgrounds
    # from being treated as skin in the normal path.
    skin = cv2.inRange(ycrcb, np.array([25, 132, 75], np.uint8), np.array([245, 180, 135], np.uint8))
    roi = np.zeros((height, width), np.uint8)
    for x, y, w, h in boxes:
        center = (max(0, min(width - 1, x + w // 2)), max(0, min(height - 1, y + h // 2)))
        axes = (max(1, int(w * 0.46)), max(1, int(h * 0.58)))
        cv2.ellipse(roi, center, axes, 0, 0, 360, 255, -1)
    if face_only:
        skin = cv2.bitwise_and(skin, roi)
    kernel = np.ones((5, 5), np.uint8)
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, kernel)
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, kernel)
    sigma = max(3.0, min(height, width) / 180.0)
    return cv2.GaussianBlur(skin, (0, 0), sigma), boxes


def analyze_faces(frames: list[np.ndarray]) -> FaceAnalysis:
    if not frames:
        return FaceAnalysis()
    face_ratios: list[float] = []
    skin_ratios: list[float] = []
    hues: list[float] = []
    luminances: list[float] = []
    skin_rgbs: list[np.ndarray] = []
    counts: list[int] = []
    for frame in frames:
        mask, boxes = skin_mask_bgr(frame)
        area = float(frame.shape[0] * frame.shape[1])
        face_ratios.append(sum(w * h for _, _, w, h in boxes) / max(area, 1.0))
        selected = mask >= 64
        skin_ratios.append(float(np.mean(selected)))
        counts.append(len(boxes))
        if np.any(selected):
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            y = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)[..., 0]
            hues.append(float(np.median(hsv[..., 0][selected])) / 179.0)
            luminances.append(float(np.median(y[selected])) / 255.0)
            skin_rgbs.append(
                np.array(
                    [float(np.median(frame[..., channel][selected])) / 255.0 for channel in (2, 1, 0)],
                    dtype=np.float64,
                )
            )
    face_count = max(counts, default=0)
    detected_ratio = float(np.mean(np.asarray(counts) > 0))
    warnings: list[str] = []
    if face_count and np.mean(skin_ratios) < 0.002:
        warnings.append("face detected but reliable skin pixels were limited")
    return FaceAnalysis(
        face_count=face_count,
        face_area_ratio=round(float(np.mean(face_ratios)), 6),
        skin_area_ratio=round(float(np.mean(skin_ratios)), 6),
        confidence=round(min(0.95, detected_ratio * 0.75 + min(0.20, np.mean(face_ratios) * 2.0)), 3),
        mean_skin_hue=round(float(np.median(hues)), 6) if hues else None,
        mean_skin_luminance=round(float(np.median(luminances)), 6) if luminances else None,
        mean_skin_rgb=(
            [round(float(value), 6) for value in np.median(np.stack(skin_rgbs), axis=0)]
            if skin_rgbs else None
        ),
        warnings=warnings,
    )
_CASCADE_LOCK = RLock()

"""Shot detection and representative-frame analysis for V0.2 projects."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from pydantic import BaseModel, Field

from color_core.correction_engine import generate_auto_correction_advice
from color_core.frame_sampler import ensure_analysis_proxy, sample_frames_at
from color_core.image_analyzer import ImageAnalysisReport, analyze_image_frames
from color_core.media_probe import MediaInfo, probe_media
from color_core.recipe import GradeRecipe
from color_core.face_analysis import FaceAnalysis, analyze_faces
from color_core.grade_decision import GradeDecision, decide_grade
from color_core.scene_embedding import scene_embedding


class SceneSegment(BaseModel):
    scene_id: str
    index: int
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    duration: float
    representative_times: list[float] = Field(default_factory=list)
    analysis: ImageAnalysisReport | None = None
    suggested_recipe: GradeRecipe | None = None
    thumbnail_path: str = ""
    face_analysis: FaceAnalysis | None = None
    grade_decision: GradeDecision | None = None
    scene_embedding: list[float] = Field(default_factory=list)
    scene_embedding_backend: str = ""
    content_class: str = "normal"
    content_flags: dict = Field(default_factory=dict)


class SceneAnalysisResult(BaseModel):
    detector: str
    threshold: float
    media_info: MediaInfo
    scenes: list[SceneSegment]
    analysis_source_path: str = ""
    acceleration: dict = Field(default_factory=dict)


MIN_REPRESENTATIVE_FRAMES = 3
MAX_REPRESENTATIVE_FRAMES = 9


def _representative_times(start: float, end: float) -> list[float]:
    """Sample more frames from longer shots.

    Three frames is enough for a static shot but not for a pan, a zoom or a
    move through changing light, where the first and last frame can describe
    very different images. The count scales with duration and is capped so a
    long take does not dominate analysis time.
    """
    duration = max(0.0, end - start)
    if duration <= 0.08:
        return [round(start, 6)]
    margin = min(0.12, duration * 0.08)
    usable_start, usable_end = start + margin, end - margin
    if usable_end <= usable_start:
        return [round(start + duration / 2.0, 6)]
    count = int(min(MAX_REPRESENTATIVE_FRAMES, max(MIN_REPRESENTATIVE_FRAMES, duration / 1.5)))
    span = usable_end - usable_start
    return [
        round(usable_start + span * (index + 0.5) / count, 6)
        for index in range(count)
    ]


def spatial_weight_map(frame: np.ndarray, content_flags: dict | None = None) -> np.ndarray:
    """Per-pixel analysis weight: centre-weighted, with matte bars excluded.

    Whole-frame statistics are dominated by whatever occupies the most area -
    sky, a dark background, letterbox bars - none of which should decide the
    exposure and white balance of the subject. ``classify_special_content``
    already detects the bars; before this they were used only to skip grading
    decisions, never to keep the bars out of the numbers that drive them.
    """
    height, width = frame.shape[:2]
    flags = content_flags or {}
    rows = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    columns = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    # Gaussian falloff, sigma ~0.7 of the half-frame: the centre counts roughly
    # three times the extreme corners, without discarding the edges outright.
    weight = np.exp(-(rows ** 2 + columns ** 2) / (2.0 * 0.7 ** 2)).astype(np.float32)

    if flags.get("letterbox"):
        band = max(1, int(height * 0.06))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        top = int(np.argmax(gray.mean(axis=1) > 0.025)) if (gray.mean(axis=1) > 0.025).any() else band
        flipped = gray[::-1].mean(axis=1) > 0.025
        bottom = int(np.argmax(flipped)) if flipped.any() else band
        weight[:top, :] = 0.0
        if bottom:
            weight[height - bottom:, :] = 0.0
    if flags.get("pillarbox"):
        band = max(1, int(width * 0.06))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        columns_mean = gray.mean(axis=0) > 0.025
        left = int(np.argmax(columns_mean)) if columns_mean.any() else band
        flipped = gray[:, ::-1].mean(axis=0) > 0.025
        right = int(np.argmax(flipped)) if flipped.any() else band
        weight[:, :left] = 0.0
        if right:
            weight[:, width - right:] = 0.0
    if not np.any(weight > 0.0):
        return np.ones((height, width), dtype=np.float32)
    return weight


def classify_special_content(frames: list[np.ndarray], report: ImageAnalysisReport | None) -> tuple[str, dict]:
    """Conservatively identify content whose darkness or bars are likely intentional."""
    if not frames or report is None:
        return "normal", {}
    gray = [cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0 for frame in frames]
    medians = [float(np.median(item)) for item in gray]
    near_black = max(medians) <= 0.025 and report.clipping.black_clipping_ratio >= 0.85
    if near_black:
        return "black_or_near_black", {
            "preserve_intent": True,
            "reason": "near-black frames are treated as intentional black/transition content",
            "median_luminance": round(max(medians), 6),
        }

    height, width = gray[0].shape
    band_h = max(1, int(height * 0.06))
    band_w = max(1, int(width * 0.06))
    top_bottom = [float((item[:band_h].mean() + item[-band_h:].mean()) / 2.0) for item in gray]
    left_right = [float((item[:, :band_w].mean() + item[:, -band_w:].mean()) / 2.0) for item in gray]
    centers = [float(item[band_h:height - band_h, band_w:width - band_w].mean()) for item in gray]
    letterbox = all(edge < 0.025 and center - edge > 0.08 for edge, center in zip(top_bottom, centers))
    pillarbox = all(edge < 0.025 and center - edge > 0.08 for edge, center in zip(left_right, centers))

    monotonic = len(medians) >= 3 and (
        all(medians[i] <= medians[i + 1] for i in range(len(medians) - 1))
        or all(medians[i] >= medians[i + 1] for i in range(len(medians) - 1))
    )
    fade = monotonic and min(medians) < 0.05 and max(medians) - min(medians) > 0.12
    if fade:
        return "fade_candidate", {
            "preserve_intent": True,
            "reason": "a monotonic fade to/from black should not receive automatic exposure lift",
            "sample_medians": [round(value, 6) for value in medians],
        }
    if letterbox or pillarbox:
        return "letterboxed", {
            "letterbox": letterbox,
            "pillarbox": pillarbox,
            "manual_review": False,
            "reason": "black bars detected; clipping metrics should be interpreted on active picture",
        }
    return "normal", {}


def detect_and_analyze_scenes(
    file_path: str,
    detector: str = "adaptive",
    threshold: float = 3.0,
    min_scene_len: int = 12,
    analyze: bool = True,
    artifact_dir: str | None = None,
) -> SceneAnalysisResult:
    """Detect shots with PySceneDetect, then analyze three frames per shot."""
    media = probe_media(file_path)
    if media.is_image:
        frames = sample_frames_at(file_path, [0.0])
        # Faces and matte bars have to be known before the recipe is computed:
        # exposure is anchored on skin when a face is present, and the bars are
        # excluded from the statistics that drive it.
        face = analyze_faces(frames) if analyze else None
        first_pass = analyze_image_frames(frames) if analyze else None
        content_class, content_flags = classify_special_content(frames, first_pass)
        analysis = (
            analyze_image_frames(frames, [spatial_weight_map(item, content_flags) for item in frames])
            if analyze else None
        )
        recipe = (
            generate_auto_correction_advice(analysis, face=face, content_flags=content_flags).suggested_recipe
            if analysis else None
        )
        decision = decide_grade(analysis, face, content_flags) if analysis else None
        embedding, embedding_backend = scene_embedding(frames)
        scene = SceneSegment(
            scene_id="scene_0001", index=0, start_time=0.0, end_time=0.0,
            start_frame=0, end_frame=1, duration=0.0, representative_times=[0.0],
            analysis=analysis, suggested_recipe=recipe, face_analysis=face, grade_decision=decision,
            scene_embedding=embedding, scene_embedding_backend=embedding_backend,
            content_class=content_class, content_flags=content_flags,
        )
        return SceneAnalysisResult(
            detector="image", threshold=threshold, media_info=media, scenes=[scene],
            analysis_source_path=file_path, acceleration={"proxy_used": False},
        )

    try:
        from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video
    except ImportError as exc:  # pragma: no cover - environment doctor checks this
        raise RuntimeError("PySceneDetect is required; install scenedetect>=0.7,<0.8") from exc

    analysis_path, acceleration = ensure_analysis_proxy(file_path)
    video = open_video(str(Path(analysis_path).resolve()))
    manager = SceneManager()
    if detector == "content":
        manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_scene_len))
    elif detector == "adaptive":
        manager.add_detector(AdaptiveDetector(adaptive_threshold=threshold, min_scene_len=min_scene_len))
    else:
        raise ValueError("detector must be 'adaptive' or 'content'")
    manager.detect_scenes(video=video, show_progress=False)
    detected = manager.get_scene_list(start_in_scene=True)
    if not detected:
        detected = [(video.base_timecode, video.duration)]

    boundaries: list[tuple[float, float, list[float]]] = []
    all_times: list[float] = []
    for start_tc, end_tc in detected:
        start, end = float(start_tc.seconds), float(end_tc.seconds)
        times = _representative_times(start, end)
        boundaries.append((start, end, times))
        all_times.extend(times)
    sampled = sample_frames_at(analysis_path, all_times) if analyze or artifact_dir else []
    thumbnails = Path(artifact_dir).resolve() / "thumbnails" if artifact_dir else None
    if thumbnails:
        thumbnails.mkdir(parents=True, exist_ok=True)

    scenes: list[SceneSegment] = []
    cursor = 0
    for index, (start, end, times) in enumerate(boundaries):
        report = None
        recipe = None
        frames = sampled[cursor:cursor + len(times)]
        cursor += len(times)
        face = analyze_faces(frames) if frames else None
        if analyze and frames:
            # First pass detects matte bars; the second re-measures with those
            # regions and the frame periphery down-weighted, so the statistics
            # describe the subject rather than whatever fills the most area.
            first_pass = analyze_image_frames(frames)
            content_class, content_flags = classify_special_content(frames, first_pass)
            report = analyze_image_frames(
                frames, [spatial_weight_map(item, content_flags) for item in frames]
            )
            recipe = generate_auto_correction_advice(
                report, face=face, content_flags=content_flags
            ).suggested_recipe
        else:
            content_class, content_flags = classify_special_content(frames, report)
        decision = decide_grade(report, face, content_flags) if report else None
        embedding, embedding_backend = scene_embedding(frames) if frames else ([], "")
        thumbnail_path = ""
        if thumbnails and frames:
            thumbnail = frames[len(frames) // 2]
            target = thumbnails / f"scene_{index + 1:04d}.jpg"
            cv2.imwrite(str(target), thumbnail, [cv2.IMWRITE_JPEG_QUALITY, 88])
            thumbnail_path = f"thumbnails/{target.name}"
        scenes.append(SceneSegment(
            scene_id=f"scene_{index + 1:04d}", index=index,
            start_time=round(start, 6), end_time=round(end, 6),
            start_frame=int(round(start * media.fps)), end_frame=int(round(end * media.fps)),
            duration=round(max(0.0, end - start), 6), representative_times=times,
            analysis=report, suggested_recipe=recipe, thumbnail_path=thumbnail_path,
            face_analysis=face, grade_decision=decision,
            scene_embedding=embedding, scene_embedding_backend=embedding_backend,
            content_class=content_class, content_flags=content_flags,
        ))
    return SceneAnalysisResult(
        detector=detector, threshold=threshold, media_info=media, scenes=scenes,
        analysis_source_path=analysis_path, acceleration=acceleration,
    )

"""Role-aware project QC for V0.6.

What changed and why
--------------------
V0.5.2 already exposed a ``transform_type`` on the LUT inspector, but every
caller passed ``"combined"``, so a technical correction LUT and a creative Look
LUT were judged by the same rules. That produced systematic, non-actionable
warnings: every shot that carried an exposure or white-balance correction
tripped "out-of-range samples" (a positive exposure legitimately pushes past
1.0) and "neutral axis colour separation" (a white balance legitimately moves
the neutral axis).

V0.6 separates the roles and, more importantly, changes *what* is measured:

``technical``
    A technical correction is allowed to move the neutral axis - that is what a
    white balance does. What it must never do is move it by a *different amount
    at different luminances*. So the metric is neutral-axis chromaticity
    **drift** (the variation along the ramp), not absolute offset. Before the
    V0.6 linear chromatic adaptation this drift measured 0.076-0.172; it is now
    float noise, and a regression would be caught here.

``creative``
    A Look may shift neutrals deliberately, so neutrality is not checked at all.
    What matters is that the transform is smooth (banding), broadly monotone,
    and does not rotate skin hue beyond a stated tolerance.

``combined``
    Both layers baked together: drift and smoothness, with a looser drift
    tolerance because the creative layer may add a mild luminance-dependent
    tint by design.

Skin safety now uses CIEDE2000 on real Lab values instead of an HSV hue scalar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from color_core.project_recipe import ProjectGradeRecipe
from color_core.scene_analysis import SceneAnalysisResult
from color_core.frame_sampler import sample_frames_at
from color_core.image_analyzer import analyze_image_frames
from color_core.face_analysis import analyze_faces
from color_core.color_metrics import skin_difference_rec709

TransformRole = Literal["technical", "creative", "combined", "output"]
QCState = Literal["PASS", "PASS_WITH_WARNINGS", "NEEDS_REVIEW", "FAIL"]

_SEVERITY = {"PASS": 0, "PASS_WITH_WARNINGS": 1, "NEEDS_REVIEW": 2, "FAIL": 3}

# Thresholds below were calibrated by baking real OCIO LUTs rather than chosen
# by intuition; the measured populations are quoted at each use site.
NEUTRAL_DRIFT_TECHNICAL_LIMIT = 0.040

# Output values below this are too close to black for their chromaticity to
# mean anything. See _neutral_axis_metrics for the measurement that set it.
NEUTRAL_AXIS_OUTPUT_FLOOR = 0.05
SKIN_ROTATION_CREATIVE_LIMIT = 12.0
SKIN_ROTATION_COMBINED_LIMIT = 18.0

# Skin safety is judged on HUE, not on total colour difference.
#
# Total Delta E contains Delta L*, so a deliberate exposure lift scored as a
# large "skin risk": across 38 flagged shots in production the correlation
# between Delta E2000 and the shot's |EV| was 0.813, and the twelve worst were
# all shots that had hit the +0.80 EV ceiling. Delta C* is contaminated the same
# way, because CIELAB chroma scales with lightness.
#
# Measured on a six-tone skin panel through real baked LUTs, Delta H* for a pure
# exposure change stays within +/-0.04 while a deliberate tint reaches 4.6 and
# cold_thriller/day_for_night reach 4.9/16.5. Hue separates cleanly; lightness
# and chroma do not.
# NOT VALIDATED AGAINST HUMAN JUDGEMENT.
#
# The 3.0 / 6.0 pair was set from colour-science reasoning alone. Checked against
# a 45-shot review, skin hue rotation did not predict whether a person wanted the
# shot reworked - it pointed the wrong way (shots marked "needs work" averaged
# 5.1 degrees, shots accepted 7.0; AUC 0.366). A second reviewer showed the same
# non-relationship.
#
# That does not mean the measurement is wrong - it is clean and
# exposure-independent on the golden set - it means this threshold flags shots
# nobody objects to. Until a targeted perceptual test establishes the real
# detection threshold, hue is reported on every shot but only gates at a level
# where the rotation is unarguable: measured on the Look packages, everything
# except cold_thriller (33 degrees) and day_for_night (114) sits below 5.
SKIN_HUE_DELTA_LIMIT = 8.0        # Delta H* in CIELAB units
SKIN_HUE_ROTATION_LIMIT = 20.0    # degrees, the vectorscope reading
SKIN_CLIP_LEVEL = 0.98            # graded skin at or above this has lost detail

# Exposure change beyond which a shot goes on the human review list.
#
# This is the one threshold in this file calibrated against people rather than
# theory. Across 45 reviewed shots it was the only measurement that separated
# "needs work" from "acceptable" (AUC 0.715), and it did so consistently for
# both reviewers - medians 0.51 EV against 0.25 and 0.32.
#
# 0.50 was chosen over the Youden-optimal 0.35 deliberately: at 0.50 precision
# is 0.82 against 0.75, and a triage list that is four-fifths real gets read,
# while a longer one gets skimmed. It selects 23 of 136 shots on the reference
# project.
LARGE_EXPOSURE_CORRECTION_EV = 0.50

# A cut has to be this much worse than it was in the source before the grade is
# blamed for it. 1.15 leaves room for measurement noise while still catching a
# grade that genuinely pushed two shots apart.
WORSENED_RATIO = 1.15


def _source_white_balance_delta(left, right) -> float:
    """The same white-balance difference, measured on the ungraded source."""
    if left is None or right is None:
        return 0.0
    if left.white_balance.source_cct_kelvin and right.white_balance.source_cct_kelvin:
        return abs(
            left.white_balance.mired_offset_from_d65 - right.white_balance.mired_offset_from_d65
        )
    return abs(
        left.white_balance.estimated_temp_offset - right.white_balance.estimated_temp_offset
    ) * 50.0

# A small panel of representative skin tones (Rec.709 encoded, light to deep).
# Creative LUTs are probed with these so hue rotation is reported as a number a
# colourist can act on rather than as a pass/fail opinion.
_SKIN_PROBES = np.array([
    [0.878, 0.729, 0.639],
    [0.800, 0.616, 0.514],
    [0.702, 0.510, 0.404],
    [0.573, 0.392, 0.302],
    [0.435, 0.286, 0.220],
    [0.318, 0.204, 0.157],
], dtype=np.float64)


def _worst(*states: str) -> QCState:
    return max(states, key=lambda item: _SEVERITY[item])  # type: ignore[return-value]


def format_timecode(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours, remainder = divmod(total, 3600.0)
    minutes, secs = divmod(remainder, 60.0)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


class LUTQuality(BaseModel):
    path: str
    size: int
    finite: bool
    in_range_ratio: float
    monotonic_violation_ratio: float
    neutral_axis_max_spread: float
    # V0.6: variation of neutral-axis chromaticity along the ramp. This is the
    # metric that distinguishes a correct white balance (uniform shift, drift
    # ~0) from the pre-V0.6 log-domain slope bug (axis fans out).
    neutral_axis_chromaticity_drift: float = 0.0
    # Second-difference magnitude. Banding correlates with curvature, not with
    # the first difference that max_gradient measures.
    max_curvature: float = 0.0
    skin_hue_rotation_deg: float = 0.0
    max_gradient: float = 0.0
    passed: bool
    state: QCState = "PASS"
    warnings: list[str] = Field(default_factory=list)
    transform_type: TransformRole = "combined"


class ProjectQualityReport(BaseModel):
    passed: bool
    final_decision: QCState = "PASS"
    render_integrity: QCState = "PASS"
    technical_color: QCState = "PASS"          # retained for compatibility
    base_correction: QCState = "PASS"          # V0.6
    creative_transform: QCState = "PASS"       # V0.6
    scene_continuity: QCState = "PASS"
    within_group_continuity: QCState = "PASS"  # V0.6
    skin_safety: QCState = "PASS"
    approval_recommended: bool = True
    scene_count: int
    shot_count: int = 0
    scene_group_count: int = 0
    adjacent_luminance_jumps: list[dict] = Field(default_factory=list)
    adjacent_temperature_jumps: list[dict] = Field(default_factory=list)
    clipping_scenes: list[dict] = Field(default_factory=list)
    # Shots the automatic exposure had to move a long way. Calibrated against
    # human review; see LARGE_EXPOSURE_CORRECTION_EV.
    large_exposure_corrections: list[dict] = Field(default_factory=list)
    skin_tone_risks: list[dict] = Field(default_factory=list)
    lut_reports: list[LUTQuality] = Field(default_factory=list)
    lut_warning_summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    review_items: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LUT inspection
# ---------------------------------------------------------------------------

def _neutral_axis_metrics(data: np.ndarray) -> tuple[float, float]:
    """Absolute neutral spread and chromaticity drift along the neutral ramp."""
    size = data.shape[0]
    diagonal = np.asarray([data[i, i, i] for i in range(size)], dtype=np.float64)
    absolute_spread = float(np.max(np.ptp(diagonal, axis=1)))
    # rg-chromaticity r/(r+g+b), g/(r+g+b). It needs no colour-space assumption
    # and is sufficient to detect an axis that opens up, which is the failure
    # mode being guarded against.
    #
    # Samples touching the domain boundary are excluded. A channel gain above
    # unity clamps at the top of the cube, and a clamped sample's chromaticity
    # is an artefact of the clamp, not of the transform - including them would
    # make every technically correct LUT with a positive gain look defective.
    #
    # Only the working mid-range is measured. Near black the ACEScct toe is a
    # linear segment rather than a power law and chromaticity is numerically
    # unstable; near white everything is against the clamp. Measured on real
    # baked LUTs, restricting to 0.15-0.95 separates legitimate technical
    # grades (max 0.026) from the pre-V0.6 log-slope defect (min 0.068), while
    # the full-range figure overlaps and cannot be thresholded.
    # The output floor matters as much as the input range. A sample can sit
    # well inside the ramp yet land near black after a negative exposure, deep
    # in the ACEScct toe where the encoding stops being a power law and tiny
    # absolute differences swing the chromaticity wildly.
    #
    # Observed on a real -0.8 EV shot: every sample from 0.13 upward agreed to
    # four decimals (0.3176, 0.3313) - a textbook uniform white balance - while
    # two samples that had been pushed down to RGB ~0.044 read 0.2326 and
    # dragged the measured drift to 0.085, over twice the threshold. The
    # transform was correct; the metric was reading noise.
    position = np.arange(size) / max(size - 1, 1)
    totals = np.sum(diagonal, axis=1)
    unclipped = (
        (totals > 1e-4)
        & np.all(diagonal < 0.999, axis=1)
        & np.all(diagonal > NEUTRAL_AXIS_OUTPUT_FLOOR, axis=1)
        & (position >= 0.15)
        & (position <= 0.95)
    )
    usable = diagonal[unclipped]
    if len(usable) < 3:
        return absolute_spread, 0.0
    chroma = usable[:, :2] / np.sum(usable, axis=1, keepdims=True)
    drift = float(np.max(np.ptp(chroma, axis=0)))
    return absolute_spread, drift


def _sample_trilinear(data: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Trilinear LUT sampling on the B,G,R volume axes.

    Nearest-node sampling is not good enough here: at 33 nodes the quantisation
    alone registered as ~7 degrees of hue rotation on an identity transform,
    which would have put a fixed offset under every skin measurement.
    """
    size = data.shape[0]
    position = np.clip(rgb[:, [2, 1, 0]], 0.0, 1.0) * (size - 1)
    low = np.floor(position).astype(int)
    high = np.minimum(low + 1, size - 1)
    fraction = position - low
    output = np.zeros((len(rgb), 3), dtype=np.float64)
    for db in (0, 1):
        bi = high[:, 0] if db else low[:, 0]
        wb = fraction[:, 0] if db else 1.0 - fraction[:, 0]
        for dg in (0, 1):
            gi = high[:, 1] if dg else low[:, 1]
            wg = fraction[:, 1] if dg else 1.0 - fraction[:, 1]
            for dr in (0, 1):
                ri = high[:, 2] if dr else low[:, 2]
                wr = fraction[:, 2] if dr else 1.0 - fraction[:, 2]
                output += data[bi, gi, ri] * (wb * wg * wr)[:, None]
    return output


def _skin_hue_rotation(data: np.ndarray) -> float:
    """Largest hue rotation (degrees) the LUT applies to the skin probe panel."""
    sampled = _sample_trilinear(data, _SKIN_PROBES)
    rotations: list[float] = []
    for probe, output in zip(_SKIN_PROBES, sampled):
        before = np.arctan2(
            np.sqrt(3.0) * (probe[1] - probe[2]), 2.0 * probe[0] - probe[1] - probe[2]
        )
        after = np.arctan2(
            np.sqrt(3.0) * (output[1] - output[2]), 2.0 * output[0] - output[1] - output[2]
        )
        delta = np.degrees(np.arctan2(np.sin(after - before), np.cos(after - before)))
        rotations.append(abs(float(delta)))
    return max(rotations) if rotations else 0.0


def inspect_cube_lut(path: str, transform_type: TransformRole = "combined") -> LUTQuality:
    """Inspect a CUBE with the tolerances appropriate to its role.

    ``transform_type`` is not cosmetic. Passing ``"combined"`` for a purely
    technical LUT is what generated the bulk of the historical false positives.
    """
    lut_path = Path(path).resolve()
    size = 0
    values: list[list[float]] = []
    for raw in lut_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("LUT_3D_SIZE"):
            size = int(line.split()[-1])
        elif line and not line.startswith("#") and not line.startswith("TITLE") and not line.startswith("DOMAIN"):
            parts = line.split()
            if len(parts) == 3:
                try:
                    values.append([float(value) for value in parts])
                except ValueError:
                    continue
    if size <= 1 or len(values) != size ** 3:
        raise ValueError(f"Invalid CUBE data: size={size}, rows={len(values)}")
    data = np.asarray(values, dtype=np.float64).reshape(size, size, size, 3)

    finite = bool(np.isfinite(data).all())
    in_range = float(np.mean((data >= -0.02) & (data <= 1.02)))
    all_diffs = [np.diff(data, axis=axis) for axis in range(3)]
    # CUBE volume axes are B,G,R while output components are R,G,B. Only the
    # corresponding primary response is expected to be mostly monotone; cross-
    # channel decreases are valid colour transforms and must not be rejected.
    primary_diffs = [all_diffs[0][..., 2], all_diffs[1][..., 1], all_diffs[2][..., 0]]
    violation = float(np.mean(np.concatenate([item.reshape(-1) for item in primary_diffs]) < -0.025))
    max_gradient = float(np.max(np.abs(np.concatenate([item.reshape(-1) for item in all_diffs]))))
    curvature = float(
        np.max(np.abs(np.concatenate([
            np.diff(data, n=2, axis=axis).reshape(-1) for axis in range(3)
        ])))
    )
    absolute_spread, drift = _neutral_axis_metrics(data)
    skin_rotation = _skin_hue_rotation(data)

    warnings: list[str] = []
    state: QCState = "PASS"

    if not finite:
        warnings.append("LUT contains non-finite values")
        state = "FAIL"

    if transform_type == "technical":
        # A technical LUT may move the neutral axis and may exceed [0,1]:
        # white balance does the former, positive exposure the latter. Neither
        # is checked. What is checked is that the axis moves uniformly.
        #
        # 0.04 sits in the measured gap between legitimate technical grades
        # (<= 0.026 across exposure, WB, contrast and saturation combinations)
        # and the pre-V0.6 log-slope defect (>= 0.068).
        if drift > NEUTRAL_DRIFT_TECHNICAL_LIMIT:
            warnings.append(
                f"technical LUT neutral axis drifts by {drift:.4f} across the ramp, "
                "a white balance should shift it uniformly"
            )
            state = _worst(state, "NEEDS_REVIEW")
        if violation > 0.02:
            warnings.append("technical LUT contains local monotonicity reversals")
            state = _worst(state, "NEEDS_REVIEW")
    elif transform_type == "creative":
        # Neutrality is deliberately not checked - a Look is allowed to tint.
        if in_range < 0.98:
            warnings.append("creative LUT maps a material share of samples outside the display domain")
            state = _worst(state, "PASS_WITH_WARNINGS")
        if violation > 0.01:
            warnings.append("creative LUT contains significant local monotonicity reversals")
            state = _worst(state, "NEEDS_REVIEW")
        if skin_rotation > SKIN_ROTATION_CREATIVE_LIMIT:
            warnings.append(
                f"creative LUT rotates skin hue by up to {skin_rotation:.1f} degrees"
            )
            state = _worst(state, "NEEDS_REVIEW")
    else:  # combined / output
        # A combined LUT carries a creative layer, which is allowed to tint
        # differently at different luminances, so drift is informational here
        # rather than diagnostic. The meaningful drift check is on the technical
        # layer - which is precisely why the roles are separated.
        if violation > 0.01:
            warnings.append("LUT contains significant local monotonicity reversals")
            state = _worst(state, "NEEDS_REVIEW")
        if skin_rotation > SKIN_ROTATION_COMBINED_LIMIT:
            warnings.append(f"LUT rotates skin hue by up to {skin_rotation:.1f} degrees")
            state = _worst(state, "NEEDS_REVIEW")

    # Hard failure stays deliberately narrow: only numerically broken or wildly
    # unstable LUTs are refused outright, because a refusal swaps the shot to a
    # fallback LUT and that is a bigger intervention than a warning.
    passed = finite and violation <= 0.03 and max_gradient <= 0.5
    if not passed:
        state = "FAIL"

    return LUTQuality(
        path=str(lut_path), size=size, finite=finite, in_range_ratio=round(in_range, 6),
        monotonic_violation_ratio=round(violation, 6),
        neutral_axis_max_spread=round(absolute_spread, 6),
        neutral_axis_chromaticity_drift=round(drift, 6),
        max_curvature=round(curvature, 6),
        skin_hue_rotation_deg=round(skin_rotation, 3),
        max_gradient=round(max_gradient, 6),
        passed=passed, state=state, warnings=warnings, transform_type=transform_type,
    )


# ---------------------------------------------------------------------------
# Project evaluation
# ---------------------------------------------------------------------------

def _skin_difference(source_face, output_face) -> dict | None:
    """Decomposed skin change, or None when either side has no skin sample."""
    if not (source_face and output_face):
        return None
    if not (source_face.mean_skin_rgb and output_face.mean_skin_rgb):
        return None
    try:
        difference = skin_difference_rec709(source_face.mean_skin_rgb, output_face.mean_skin_rgb)
    except Exception:
        return None
    difference["clipped"] = bool(max(output_face.mean_skin_rgb) >= SKIN_CLIP_LEVEL)
    return difference


def _group_id(project: ProjectGradeRecipe, shot) -> str:
    if shot is None:
        return ""
    group = project.group_for_shot(shot.shot_id or shot.scene_id)
    return group.scene_group_id if group else ""


def evaluate_project_quality(
    project: ProjectGradeRecipe,
    analysis: SceneAnalysisResult,
    lut_paths: list | None = None,
    rendered_path: str | None = None,
) -> ProjectQualityReport:
    """Evaluate a rendered project.

    ``lut_paths`` accepts either bare paths (treated as ``combined``) or
    ``(path, transform_type)`` pairs. Callers that know the role should pass
    pairs - that is the whole point of the V0.6 split.
    """
    lum_limit = float(project.qc_constraints.get("max_adjacent_luminance_jump", 0.25))
    temp_limit = float(project.qc_constraints.get("max_adjacent_temperature_jump", 12.0))
    # 25 mired between adjacent shots is about an eighth of a CTO - the point a
    # colour-temperature cut starts reading as a mismatch rather than a look.
    mired_limit = float(project.qc_constraints.get("max_adjacent_mired_jump", 25.0))
    clip_limit = float(project.qc_constraints.get("max_highlight_clipping_ratio", 0.06))
    skin_hue_limit = float(project.qc_constraints.get("max_skin_delta_hue", SKIN_HUE_DELTA_LIMIT))

    lum_jumps: list[dict] = []
    temp_jumps: list[dict] = []
    clipping: list[dict] = []
    large_exposure: list[dict] = []
    reports = [scene.analysis for scene in analysis.scenes]
    output_faces: list = [None for _ in analysis.scenes]
    if rendered_path and analysis.scenes:
        times: list[float] = []
        for scene in analysis.scenes:
            duration = max(0.001, scene.end_time - scene.start_time)
            times.extend([scene.start_time + duration * part for part in (0.2, 0.5, 0.8)])
        frames = sample_frames_at(rendered_path, times, target_height=720)
        reports = []
        output_faces = []
        for index in range(len(analysis.scenes)):
            scene_frames = frames[index * 3:index * 3 + 3]
            reports.append(analyze_image_frames(scene_frames))
            output_faces.append(analyze_faces(scene_frames))

    skin_risks: list[dict] = []
    for index, report in enumerate(reports):
        scene = analysis.scenes[index]
        shot = project.shots[index] if index < len(project.shots) else None
        locator = {
            "scene_id": scene.scene_id,
            "shot_id": shot.shot_id if shot else "",
            "scene_group_id": _group_id(project, shot),
            "start_time": scene.start_time,
            "end_time": scene.end_time,
            "timecode": format_timecode(scene.start_time),
        }
        if report and report.clipping.highlight_clipping_ratio > clip_limit:
            clipping.append({**locator, "ratio": report.clipping.highlight_clipping_ratio})

        # The human-calibrated flag: a shot the automatic pass had to move a long
        # way is the shot a person most often wants to redo.
        if shot is not None:
            diagnostics = shot.base_correction.exposure_diagnostics or {}
            applied = abs(float(shot.base_correction.exposure))
            requested = abs(float(diagnostics.get("requested_ev", applied)))
            if max(applied, requested) >= LARGE_EXPOSURE_CORRECTION_EV:
                large_exposure.append({
                    **locator,
                    "applied_ev": round(float(shot.base_correction.exposure), 3),
                    "requested_ev": round(requested, 3),
                    "anchor": diagnostics.get("anchor", ""),
                    "highlight_limited": bool(diagnostics.get("highlight_limited", False)),
                    "reason": (
                        f"automatic exposure moved this shot by {applied:.2f} EV"
                        + (f" (measurement asked for {requested:.2f})" if requested > applied + 0.05 else "")
                    ),
                    "suggested_action": "local_correction",
                })

        source_face = scene.face_analysis
        output_face = output_faces[index]
        if source_face and source_face.face_count and output_face and not output_face.face_count:
            skin_risks.append({
                **locator,
                "reason": "face detector lost the source face after grading",
                "suggested_action": "review_shot",
            })
        difference = _skin_difference(source_face, output_face)
        if difference is not None:
            measurements = {
                "skin_delta_e2000": difference["delta_e2000"],
                "skin_delta_lightness": difference["delta_lightness"],
                "skin_delta_chroma": difference["delta_chroma"],
                "skin_delta_hue": difference["delta_hue"],
                "skin_hue_rotation_deg": difference["hue_rotation_deg"],
            }
            if difference["clipped"]:
                # Distinct from a hue shift and far more damaging: once skin is
                # at the top of the range its colour is gone, not merely moved.
                skin_risks.append({
                    **locator, **measurements,
                    "category_detail": "skin_clipped",
                    "reason": "graded skin reaches the top of the display range and has lost detail",
                    "suggested_action": "reduce_exposure",
                })
            elif (
                abs(difference["delta_hue"]) > skin_hue_limit
                or abs(difference["hue_rotation_deg"]) > SKIN_HUE_ROTATION_LIMIT
            ):
                skin_risks.append({
                    **locator, **measurements,
                    "category_detail": "skin_hue_shift",
                    "reason": (
                        f"skin hue rotated {difference['hue_rotation_deg']:+.1f} degrees "
                        f"(dH* {difference['delta_hue']:+.2f}); lightness change "
                        f"({difference['delta_lightness']:+.1f} L*) is not counted"
                    ),
                    "suggested_action": "lower_look_strength" if (shot and shot.look_strength > 0.0) else "review_shot",
                })

        if index == 0 or report is None or reports[index - 1] is None:
            continue
        # A transition or an intentional black frame is not a continuity defect.
        if (scene.content_flags or {}).get("preserve_intent") or (
            analysis.scenes[index - 1].content_flags or {}
        ).get("preserve_intent"):
            continue
        previous = reports[index - 1]

        def _white_balance_delta() -> tuple[float, float]:
            """Return (measured delta, limit) for the white-balance jump check.

            Mired is used whenever both shots carry a real illuminant estimate;
            the legacy gain-difference metric is only a fallback for reports
            produced before V0.6.2.
            """
            if report.white_balance.source_cct_kelvin and previous.white_balance.source_cct_kelvin:
                return (
                    abs(
                        report.white_balance.mired_offset_from_d65
                        - previous.white_balance.mired_offset_from_d65
                    ),
                    mired_limit,
                )
            legacy = abs(
                report.white_balance.estimated_temp_offset
                - previous.white_balance.estimated_temp_offset
            ) * 50.0
            return legacy, temp_limit

        left = project.shots[index - 1] if index - 1 < len(project.shots) else None
        left_group_id = _group_id(project, left)
        right_group_id = locator["scene_group_id"]
        within_group = bool(left_group_id and right_group_id and left_group_id == right_group_id)
        edge = {
            "from": analysis.scenes[index - 1].scene_id, "to": scene.scene_id,
            "from_shot_id": left.shot_id if left else "", "to_shot_id": shot.shot_id if shot else "",
            "timecode": format_timecode(scene.start_time), "start_time": scene.start_time,
            "within_scene_group": within_group, "scene_group_id": right_group_id,
        }
        lum = abs(report.luminance.median - previous.luminance.median)
        temp, applicable_limit = _white_balance_delta()

        # A cut is only a continuity defect if the grade made it worse.
        #
        # Judging the output against an absolute threshold misreports the
        # result: on a real 136-shot project the source already exceeded the
        # white-balance threshold at 65 of 135 cuts, the graded output at 29 -
        # the grade halved them - yet the report read "29 problems". Of those
        # 29, sixteen were closer together than in the source. What matters is
        # the eleven the grade pushed apart.
        source_lum = abs(
            (analysis.scenes[index].analysis.luminance.median if analysis.scenes[index].analysis else 0.0)
            - (analysis.scenes[index - 1].analysis.luminance.median if analysis.scenes[index - 1].analysis else 0.0)
        )
        source_temp = _source_white_balance_delta(
            analysis.scenes[index - 1].analysis, analysis.scenes[index].analysis
        )
        if not rendered_path:
            # Nothing was rendered, so there is no "after" to compare against.
            # Fall back to absolute thresholds: this is a check on the plan.
            source_lum = source_temp = 0.0
        if lum > lum_limit and lum > source_lum * WORSENED_RATIO:
            lum_jumps.append({
                **edge, "delta": round(lum, 6), "source_delta": round(source_lum, 6),
                "worsened_by": round(lum - source_lum, 6),
                "suggested_action": "match_hero" if within_group else "review_boundary",
            })
        if temp > applicable_limit and temp > source_temp * WORSENED_RATIO:
            temp_jumps.append({
                **edge, "delta": round(temp, 6), "source_delta": round(source_temp, 6),
                "worsened_by": round(temp - source_temp, 6),
                "unit": "mired" if applicable_limit == mired_limit else "legacy_gain_delta",
                "suggested_action": "match_hero" if within_group else "review_boundary",
            })

    # ---- LUT inspection, by role ------------------------------------------
    normalized: list[tuple[str, str]] = []
    for entry in (lut_paths or []):
        if isinstance(entry, (tuple, list)):
            normalized.append((str(entry[0]), str(entry[1])))
        else:
            normalized.append((str(entry), "combined"))
    lut_reports = [inspect_cube_lut(path, role) for path, role in normalized]  # type: ignore[arg-type]

    def _role_state(role: str) -> QCState:
        states = [item.state for item in lut_reports if item.transform_type == role]
        return _worst("PASS", *states) if states else "PASS"

    base_correction = _role_state("technical")
    creative_transform = _worst(_role_state("creative"), _role_state("combined"), _role_state("output"))

    within_lum = [item for item in lum_jumps if item["within_scene_group"]]
    within_temp = [item for item in temp_jumps if item["within_scene_group"]]
    cross_lum = [item for item in lum_jumps if not item["within_scene_group"]]
    cross_temp = [item for item in temp_jumps if not item["within_scene_group"]]

    warnings: list[str] = []
    if within_lum or within_temp:
        warnings.append(
            f"{len(within_lum)} luminance and {len(within_temp)} white-balance jump(s) "
            "occur INSIDE a scene group and should be matched or regrouped"
        )
    if cross_lum or cross_temp:
        warnings.append(
            f"{len(cross_lum)} luminance and {len(cross_temp)} white-balance jump(s) "
            "occur at scene-group boundaries, which is often intentional"
        )
    if clipping:
        warnings.append(f"{len(clipping)} shot(s) exceed the highlight clipping warning threshold")
    if skin_risks:
        warnings.append(f"{len(skin_risks)} shot(s) show a material skin-tone shift")
    if large_exposure:
        warnings.append(
            f"{len(large_exposure)} shot(s) needed a large automatic exposure change "
            "and are the most likely to want a local correction"
        )

    errors = [f"LUT failed quality checks: {item.path}" for item in lut_reports if not item.passed]

    # Inside a group the shots are supposed to share one look, so a jump there
    # is a real defect. At a group boundary it is usually a deliberate cut.
    within_group_continuity: QCState = "NEEDS_REVIEW" if (within_lum or within_temp) else "PASS"
    scene_continuity = _worst(
        within_group_continuity,
        "PASS_WITH_WARNINGS" if (cross_lum or cross_temp) else "PASS",
    )
    skin_safety: QCState = "NEEDS_REVIEW" if skin_risks else "PASS"
    if clipping:
        base_correction = _worst(base_correction, "NEEDS_REVIEW")
    if errors:
        base_correction = "FAIL"
    render_integrity: QCState = "PASS" if rendered_path else "PASS_WITH_WARNINGS"
    technical_color = _worst(base_correction, creative_transform)
    final_decision = _worst(
        render_integrity, base_correction, creative_transform, scene_continuity, skin_safety
    )

    review_items = [
        *[{"category": "continuity_luminance", **item} for item in lum_jumps],
        *[{"category": "continuity_white_balance", **item} for item in temp_jumps],
        *[{"category": "highlight_clipping", **item} for item in clipping],
        *[{"category": "skin_safety", **item} for item in skin_risks],
        *[{"category": "large_exposure_correction", **item} for item in large_exposure],
    ]
    review_items.sort(key=lambda item: float(item.get("start_time", 0.0)))

    summary: dict = {}
    for item in lut_reports:
        bucket = summary.setdefault(
            item.transform_type, {"count": 0, "with_warnings": 0, "failed": 0, "warnings": {}}
        )
        bucket["count"] += 1
        bucket["with_warnings"] += 1 if item.warnings else 0
        bucket["failed"] += 0 if item.passed else 1
        for warning in item.warnings:
            key = warning.split(",")[0][:70]
            bucket["warnings"][key] = bucket["warnings"].get(key, 0) + 1

    return ProjectQualityReport(
        passed=final_decision in {"PASS", "PASS_WITH_WARNINGS"},
        final_decision=final_decision, render_integrity=render_integrity,
        technical_color=technical_color, base_correction=base_correction,
        creative_transform=creative_transform, scene_continuity=scene_continuity,
        within_group_continuity=within_group_continuity, skin_safety=skin_safety,
        approval_recommended=final_decision in {"PASS", "PASS_WITH_WARNINGS"},
        scene_count=len(analysis.scenes), shot_count=len(analysis.scenes),
        scene_group_count=len(project.scene_groups),
        adjacent_luminance_jumps=lum_jumps, adjacent_temperature_jumps=temp_jumps,
        clipping_scenes=clipping, large_exposure_corrections=large_exposure,
        skin_tone_risks=skin_risks, lut_reports=lut_reports,
        lut_warning_summary=summary, warnings=warnings, errors=errors, review_items=review_items,
    )

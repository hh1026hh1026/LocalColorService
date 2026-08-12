"""Scene-group white balance harmonisation (V0.6.2).

The problem
-----------
``image_analyzer`` estimates white balance per shot from an ensemble of
gray-world, shades-of-gray and neutral-highlight candidates. When those three
disagree it reports low confidence and the estimator gives up entirely::

    else:
        gain_r, gain_g, gain_b = 1.0, 1.0, 1.0
        selected_method = "preserve_low_confidence"

Giving up is safe for one shot in isolation, but it is exactly the wrong thing
for a sequence. The shots where the estimate is hardest - mixed lighting, a
close-up filling frame with one colour, a shot of a wall - are the ones that end
up uncorrected while their neighbours in the same lighting setup are corrected.
The project review measured the result: 15 white-balance jumps, 14 of them
*inside* a scene group.

What a colourist does instead
-----------------------------
They do not re-judge white balance shot by shot. They establish the white point
for the setup, then match everything in it to that. A shot that is hard to read
inherits the scene's answer rather than getting no answer.

Implementation
--------------
Per SceneGroup, a confidence-weighted consensus white point is computed in
**mired and Duv** - the axes on which colour temperature is perceptually even -
and each shot is then pulled toward it:

* a shot that gave up entirely adopts the consensus outright
* a confident shot shrinks toward it, keeping most of its own reading
* a confident shot that disagrees *strongly* is left alone and flagged, because
  that usually means a genuinely different light source (a practical in frame, a
  window) and forcing it would be worse than the jump

Shots the grade decision marked ``preserve`` (black frames, fades) are excluded
from both the consensus and the adjustment.
"""

from __future__ import annotations

import numpy as np

from color_core.project_recipe import ProjectGradeRecipe
from color_core.scene_analysis import SceneAnalysisResult
from color_core.white_balance import (
    cct_duv_from_white,
    gains_from_source_white,
    limit_white_point_shift,
    source_white_from_gains,
    white_from_cct_duv,
)

# A confident shot keeps this much of its own reading; the remainder is pulled
# toward the group consensus. Full replacement would erase real intra-scene
# variation, none at all is the behaviour being fixed.
CONFIDENT_SELF_WEIGHT = 0.70

# Below this the shot's own estimate is not trusted at all.
LOW_CONFIDENCE_THRESHOLD = 0.55

# A confident shot further than this from the consensus is treated as a
# genuinely different light source rather than an error to be corrected.
# 60 mired is roughly a third of a CTO - well beyond estimator noise.
DIFFERENT_LIGHT_MIRED = 60.0
DIFFERENT_LIGHT_DUV = 0.012

# How far the correction may move a shot's white point is bounded in mired by
# white_balance.limit_white_point_shift. Clamping gains instead saturates and
# manufactures discontinuities at cuts; see that function for the measurements.

# How much more different than the source a cut is allowed to become. Small
# enough to stop the grade inventing jumps, large enough not to fight a
# legitimate scene change.
BOUNDARY_TOLERANCE_MIRED = 12.0
RELAXATION_PASSES = 12


def _shot_confidence(report) -> float:
    """How much the per-shot estimate should be trusted, in [0, 1]."""
    if report is None:
        return 0.0
    balance = report.white_balance
    if balance.selected_method == "preserve_low_confidence":
        return 0.0
    return float(
        np.clip(balance.gray_world_confidence * (0.5 + 0.5 * balance.candidate_agreement), 0.0, 1.0)
    )


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted median - robust to a single wildly wrong shot in the group."""
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights)
    if cumulative[-1] <= 0:
        return float(np.median(values))
    return float(values[int(np.searchsorted(cumulative, cumulative[-1] / 2.0))])


def harmonize_group_white_balance(
    project: ProjectGradeRecipe, analysis: SceneAnalysisResult,
) -> list[dict]:
    """Pull each shot's white balance toward its scene group's consensus.

    Mutates ``project.shots[*].base_correction.rgb_gains`` in place and returns
    one diagnostic record per scene group.
    """
    scenes = {scene.scene_id: scene for scene in analysis.scenes}
    shots_by_id = {(shot.shot_id or shot.scene_id): shot for shot in project.shots}
    diagnostics: list[dict] = []
    # Targets are collected first so boundary continuity can be enforced across
    # groups before anything is written to a recipe.
    pending: list[dict] = []
    # Groups whose own shots were all unreadable; resolved from neighbours below.
    unresolved: list[tuple[str, list[dict], dict]] = []
    resolved: dict[str, tuple[float, float]] = {}

    for group in project.scene_groups:
        members = []
        for shot_id in group.shot_ids:
            shot = shots_by_id.get(shot_id)
            scene = scenes.get(shot.scene_id) if shot else None
            if shot is None or scene is None or scene.analysis is None:
                continue
            if shot.base_grade_policy == "preserve":
                continue
            if (scene.content_flags or {}).get("preserve_intent"):
                continue
            white = source_white_from_gains(shot.base_correction.rgb_gains)
            cct, duv = cct_duv_from_white(white)
            members.append({
                "shot": shot,
                "shot_id": shot_id,
                "mired": 1e6 / max(cct, 1.0),
                "duv": duv,
                "confidence": _shot_confidence(scene.analysis),
            })

        record = {
            "scene_group_id": group.scene_group_id,
            "shot_count": len(members),
            "adjusted": 0,
            "adopted_consensus": 0,
            "flagged_different_light": [],
        }
        if not members:
            diagnostics.append(record)
            continue

        confident = [item for item in members if item["confidence"] >= LOW_CONFIDENCE_THRESHOLD]
        if not confident:
            # Nobody in this group could read its own light. Leaving the group
            # alone is worse than it sounds: its neighbours in the cut do get
            # corrected, so the group becomes an uncorrected island and every
            # boundary with it turns into a jump. Measured on a real project,
            # five of twenty-five groups were in this state and they accounted
            # for the largest introduced discontinuities.
            #
            # The group is parked here and filled in from its temporal
            # neighbours once every other group has a consensus.
            record["consensus"] = None
            record["reason"] = "no confident estimate in this group; will inherit from neighbours"
            unresolved.append((group.scene_group_id, members, record))
            diagnostics.append(record)
            continue

        mireds = np.asarray([item["mired"] for item in confident], dtype=np.float64)
        duvs = np.asarray([item["duv"] for item in confident], dtype=np.float64)
        weights = np.asarray([item["confidence"] for item in confident], dtype=np.float64)
        consensus_mired = _weighted_median(mireds, weights)
        consensus_duv = _weighted_median(duvs, weights)
        record["consensus"] = {
            "cct_kelvin": round(1e6 / max(consensus_mired, 1e-6), 1),
            "mired": round(consensus_mired, 2),
            "duv": round(consensus_duv, 6),
            "contributing_shots": len(confident),
        }
        resolved[group.scene_group_id] = (consensus_mired, consensus_duv)

        for item in members:
            delta_mired = item["mired"] - consensus_mired
            delta_duv = item["duv"] - consensus_duv
            confident_enough = item["confidence"] >= LOW_CONFIDENCE_THRESHOLD
            far_from_consensus = (
                abs(delta_mired) > DIFFERENT_LIGHT_MIRED or abs(delta_duv) > DIFFERENT_LIGHT_DUV
            )
            if far_from_consensus:
                # Flagged regardless of confidence. A shot this far from its
                # group is either lit differently or badly estimated, and in both
                # cases a correction of this size should be seen by a human
                # rather than applied silently. Only the confident case is left
                # untouched - an unconfident one still takes the consensus,
                # because its own reading is the less trustworthy of the two.
                record["flagged_different_light"].append({
                    "shot_id": item["shot_id"],
                    "cct_kelvin": round(1e6 / max(item["mired"], 1e-6), 1),
                    "delta_mired": round(delta_mired, 2),
                    "confident": confident_enough,
                    "action": "left_unchanged" if confident_enough else "pulled_to_consensus",
                    "reason": (
                        "confident estimate far from the group consensus; likely a "
                        "genuinely different light source, left unchanged"
                        if confident_enough else
                        "uncertain estimate far from the group consensus; the consensus "
                        "was applied, but the size of the change warrants review"
                    ),
                })
                if confident_enough:
                    continue

            self_weight = CONFIDENT_SELF_WEIGHT if confident_enough else 0.0
            target_mired = item["mired"] * self_weight + consensus_mired * (1.0 - self_weight)
            target_duv = item["duv"] * self_weight + consensus_duv * (1.0 - self_weight)
            if abs(target_mired - item["mired"]) < 0.5 and abs(target_duv - item["duv"]) < 1e-4:
                continue

            pending.append({
                "shot": item["shot"], "shot_id": item["shot_id"], "group": group.scene_group_id,
                "source_mired": item["mired"], "source_duv": item["duv"],
                "target_mired": target_mired, "target_duv": target_duv,
                "record": record, "confident": confident_enough,
            })

        diagnostics.append(record)

    _inherit_from_neighbours(project, unresolved, resolved, pending)
    _relax_group_boundaries(project, pending)

    for item in pending:
        # The consensus can legitimately describe a strong cast, but how far the
        # white point is actually moved is bounded - in mired, not by clamping
        # gains, which saturates and creates discontinuities of its own.
        limited = limit_white_point_shift(item["source_mired"], item["target_mired"])
        gains = gains_from_source_white(
            white_from_cct_duv(1e6 / max(limited, 1e-6), item["target_duv"])
        )
        rounded = [round(value, 4) for value in gains]
        previous = list(item["shot"].base_correction.rgb_gains)
        # Every shot in a group is written the group's gains, unconditionally.
        # Skipping the write when a shot's own reading happened to sit near the
        # consensus left it holding its individual gains, so the group stopped
        # being internally consistent - the one thing the consensus exists to
        # guarantee.
        item["shot"].base_correction = item["shot"].base_correction.model_copy(
            update={"rgb_gains": rounded}
        )
        if max(abs(a - b) for a, b in zip(rounded, previous)) > 1e-4:
            item["record"]["adjusted"] += 1
            if not item["confident"]:
                item["record"]["adopted_consensus"] += 1
    return diagnostics


def _inherit_from_neighbours(
    project: ProjectGradeRecipe,
    unresolved: list[tuple[str, list[dict], dict]],
    resolved: dict[str, tuple[float, float]],
    pending: list[dict],
) -> None:
    """Give a group that could not read its own light the white point of its neighbours.

    A group whose shots all failed to produce a confident estimate used to be
    skipped entirely, which left it uncorrected while everything around it was
    corrected - so it became an island and every cut into or out of it turned
    into a colour-temperature jump.

    Borrowing from the groups it actually touches in the cut is the same
    reasoning that motivates the per-shot consensus, applied one level up.
    """
    if not unresolved or not resolved:
        return
    group_of: dict[str, str] = {}
    for group in project.scene_groups:
        for shot_id in group.shot_ids:
            group_of[shot_id] = group.scene_group_id
    ordered = [shot.shot_id or shot.scene_id for shot in project.shots]

    neighbours: dict[str, list[str]] = {}
    for left_id, right_id in zip(ordered, ordered[1:]):
        left, right = group_of.get(left_id), group_of.get(right_id)
        if not left or not right or left == right:
            continue
        neighbours.setdefault(left, []).append(right)
        neighbours.setdefault(right, []).append(left)

    for group_id, members, record in unresolved:
        candidates = [
            resolved[name] for name in neighbours.get(group_id, []) if name in resolved
        ]
        if not candidates:
            record["reason"] = "no confident estimate here or in any adjoining group; left unchanged"
            continue
        inherited_mired = float(np.median([item[0] for item in candidates]))
        inherited_duv = float(np.median([item[1] for item in candidates]))
        record["consensus"] = {
            "cct_kelvin": round(1e6 / max(inherited_mired, 1e-6), 1),
            "mired": round(inherited_mired, 2),
            "duv": round(inherited_duv, 6),
            "contributing_shots": 0,
            "inherited_from_neighbours": len(candidates),
        }
        record["reason"] = (
            f"no confident estimate in this group; inherited the white point of "
            f"{len(candidates)} adjoining group(s)"
        )
        resolved[group_id] = (inherited_mired, inherited_duv)
        for item in members:
            pending.append({
                "shot": item["shot"], "shot_id": item["shot_id"], "group": group_id,
                "source_mired": item["mired"], "source_duv": item["duv"],
                "target_mired": inherited_mired, "target_duv": inherited_duv,
                "record": record, "confident": False,
            })


def _relax_group_boundaries(project: ProjectGradeRecipe, pending: list[dict]) -> None:
    """Stop the grade inventing colour-temperature jumps that were not in the source.

    Each scene group is corrected toward its own consensus white point, with no
    knowledge of what sits next to it in the cut. Two adjacent shots that were
    nearly identical in the source can therefore be pushed far apart simply
    because they landed in different groups. Measured on a real project, one cut
    went from 4.5 mired in the source to 41.6 after grading, and eleven cuts were
    made materially worse this way.

    The constraint applied here is deliberately modest: a cut may end up as
    different as it was in the source, plus a small tolerance, and no more.
    Genuine scene changes keep their difference; invented ones are removed.

    Relaxation runs at *group* level rather than per shot, so a group never loses
    its internal consistency in order to satisfy a boundary.
    """
    if len(pending) < 2:
        return
    by_shot = {item["shot_id"]: item for item in pending}
    groups: dict[str, list[dict]] = {}
    for item in pending:
        groups.setdefault(item["group"], []).append(item)

    # Boundaries actually present in the cut, as (left group, right group).
    boundaries: list[tuple[str, str, float]] = []
    ordered = [shot.shot_id or shot.scene_id for shot in project.shots]
    for left_id, right_id in zip(ordered, ordered[1:]):
        left, right = by_shot.get(left_id), by_shot.get(right_id)
        if left is None or right is None or left["group"] == right["group"]:
            continue
        boundaries.append(
            (left["group"], right["group"], abs(left["source_mired"] - right["source_mired"]))
        )
    if not boundaries:
        return

    for _ in range(RELAXATION_PASSES):
        moved = False
        for left_group, right_group, source_delta in boundaries:
            left_items, right_items = groups.get(left_group), groups.get(right_group)
            if not left_items or not right_items:
                continue
            left_target = left_items[0]["target_mired"]
            right_target = right_items[0]["target_mired"]
            allowed = source_delta + BOUNDARY_TOLERANCE_MIRED
            delta = left_target - right_target
            if abs(delta) <= allowed:
                continue
            shift = (abs(delta) - allowed) / 2.0 * (1.0 if delta > 0 else -1.0)
            for entry in left_items:
                entry["target_mired"] -= shift
            for entry in right_items:
                entry["target_mired"] += shift
            moved = True
        if not moved:
            break

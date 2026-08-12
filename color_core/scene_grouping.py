"""SceneGroup V2 - constrained agglomerative grouping for V0.6 GradePlans.

Why V1 was replaced
-------------------
V1 walked the shot list once and merged each shot into the previous partition if
it was similar enough, subject to ``max_group_shots``:

    for shot in shots[1:]:
        if len(current) < max_group_shots and distance(current[-1], shot) <= t:
            current.append(shot)
        else:
            start_a_new_partition()

Three structural problems followed from that:

1. **Only adjacent shots could ever join.** The A-B-A-B shot/reverse-shot of a
   dialogue scene is the single most common structure in edited footage, and the
   V1 scan broke the group at every camera change. Shots that belong to one
   lighting setup ended up in different groups.
2. **``max_group_shots`` drove the result.** Because it was inside the merge
   condition, long uniform scenes were chopped into fixed 12-shot blocks - which
   is exactly the "several groups have exactly 12 shots" pattern the project
   review flagged.
3. **The distance was built on unphysical quantities.** ``estimated_temp_offset``
   was ``gain_b - gain_r``, a dimensionless invention.

V2 design
---------
* Distance uses **mired and Duv** derived through the V0.6 chromatic adaptation
  module, so the colour-temperature axis is perceptually even and physically
  meaningful, plus luminance, chroma, composition embedding and face presence.
* Grouping is **constrained agglomerative clustering**: repeatedly merge the two
  most similar clusters whose time spans are within a tolerance window. Merging
  is not restricted to neighbours, so A-B-A recombines, while the time window
  still prevents visually similar but unrelated scenes an hour apart from
  joining.
* ``max_group_shots`` is a **performance guard only**. It blocks a merge that
  would exceed it, but never forces a split of an otherwise coherent group.
* Hero shot selection is dominated by **distance to the group's own statistical
  centre**, because the hero is the reference every other shot is matched to. A
  long shot at the edge of the group's colour distribution is a bad reference.
* A **post-check** measures within-group mired and luminance spread and splits
  groups that stayed too heterogeneous. This closes the loop that the project
  review asked for: the grouper validates its own output.

Everything is deterministic - no random initialisation, no iteration order
dependence - because GradePlan reproducibility depends on it.
"""

from __future__ import annotations

import math

import numpy as np

from color_core.project_recipe import SceneGroup
from color_core.scene_analysis import SceneAnalysisResult, SceneSegment
from color_core.white_balance import describe_white_balance

# Shots further apart in time than this are never merged, however similar they
# look. Generous enough to span a dialogue scene, short enough to keep a
# recurring location in a different reel from collapsing into one group.
DEFAULT_TIME_WINDOW_SECONDS = 45.0

# Feature weights. Colour temperature carries the most weight because it is the
# dimension the project review found least consistent inside groups.
_WEIGHTS = {
    "mired": 0.30,
    "duv": 0.12,
    "luminance": 0.22,
    "chroma": 0.10,
    "embedding": 0.19,
    "face": 0.07,
}

# Spread limits used by the post-check, in feature units before weighting.
MAX_GROUP_MIRED_SPREAD = 42.0      # ~ 1/4 CTO across a group
MAX_GROUP_LUMINANCE_STOPS = 1.35


class ShotFeature:
    """Physically meaningful descriptor of one shot."""

    __slots__ = ("index", "mired", "duv", "log_luminance", "chroma", "embedding", "face_ratio", "midpoint")

    def __init__(self, shot: SceneSegment):
        self.index = shot.index
        self.midpoint = (shot.start_time + shot.end_time) / 2.0
        report = shot.analysis
        if report is None:
            self.mired = 1e6 / 6504.0
            self.duv = 0.0
            self.log_luminance = 0.0
            self.chroma = 0.0
        else:
            balance = report.white_balance
            if balance.source_cct_kelvin > 100.0:
                # Computed once during analysis since V0.6.2.
                self.mired = 1e6 / balance.source_cct_kelvin
                self.duv = float(balance.source_duv)
            else:
                try:
                    described = describe_white_balance(
                        [balance.gain_r, balance.gain_g, balance.gain_b], 0.0, 0.0
                    )
                    cct = float(described["source_cct_kelvin"])
                    self.mired = 1e6 / cct if cct > 100.0 else 1e6 / 6504.0
                    self.duv = float(described["source_duv"])
                except Exception:
                    self.mired = 1e6 / 6504.0
                    self.duv = 0.0
            self.log_luminance = math.log2(max(report.luminance.median, 0.004))
            self.chroma = report.saturation.mean
        self.embedding = np.asarray(shot.scene_embedding or [], dtype=np.float64)
        self.face_ratio = shot.face_analysis.face_area_ratio if shot.face_analysis else 0.0

    @classmethod
    def centroid(cls, items: list["ShotFeature"]) -> "ShotFeature":
        """Robust statistical centre of a set of shots (medians, not means)."""
        centre = cls.__new__(cls)
        centre.index = -1
        centre.midpoint = 0.0
        centre.mired = float(np.median([item.mired for item in items]))
        centre.duv = float(np.median([item.duv for item in items]))
        centre.log_luminance = float(np.median([item.log_luminance for item in items]))
        centre.chroma = float(np.median([item.chroma for item in items]))
        centre.face_ratio = float(np.median([item.face_ratio for item in items]))
        embeddings = [item.embedding for item in items if item.embedding.size]
        sizes = {item.size for item in embeddings}
        centre.embedding = (
            np.mean(embeddings, axis=0) if len(sizes) == 1 else np.asarray([], dtype=np.float64)
        )
        return centre


def _cosine_distance(left: np.ndarray, right: np.ndarray) -> float | None:
    """Cosine distance, or None when no embedding is available for comparison.

    Returning None rather than a fixed mid value matters: a constant penalty
    would silently consume a large share of the merge threshold whenever
    embeddings are missing, so nothing would ever merge.
    """
    if left.size == 0 or left.size != right.size:
        return None
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator < 1e-9:
        return None
    return float(np.clip(1.0 - float(left @ right) / denominator, 0.0, 1.0))


class _Scales:
    """Robust per-dimension normalisation so no axis dominates by unit choice."""

    def __init__(self, features: list[ShotFeature]):
        def spread(values: list[float], floor: float) -> float:
            if len(values) < 2:
                return floor
            array = np.asarray(values, dtype=np.float64)
            median = float(np.median(array))
            mad = float(np.median(np.abs(array - median))) * 1.4826
            return max(mad, floor)

        self.mired = spread([item.mired for item in features], 12.0)
        self.duv = spread([item.duv for item in features], 0.004)
        self.luminance = spread([item.log_luminance for item in features], 0.45)
        self.chroma = spread([item.chroma for item in features], 0.06)


def _feature_distance(left: ShotFeature, right: ShotFeature, scales: _Scales) -> float:
    embedding = _cosine_distance(left.embedding, right.embedding)
    terms: list[tuple[float, float]] = [
        (_WEIGHTS["mired"], abs(left.mired - right.mired) / scales.mired),
        (_WEIGHTS["duv"], abs(left.duv - right.duv) / scales.duv),
        (_WEIGHTS["luminance"], abs(left.log_luminance - right.log_luminance) / scales.luminance),
        (_WEIGHTS["chroma"], abs(left.chroma - right.chroma) / scales.chroma),
        (_WEIGHTS["face"], min(1.0, abs(left.face_ratio - right.face_ratio) / 0.20)),
    ]
    if embedding is not None:
        terms.append((_WEIGHTS["embedding"], embedding / 0.45))
    # Weights are renormalised over whatever is actually available so a missing
    # embedding changes precision, not the scale of the distance.
    total_weight = sum(weight for weight, _ in terms)
    return sum(weight * min(value, 3.0) for weight, value in terms) / max(total_weight, 1e-9)


def _cluster_shots(
    features: list[ShotFeature],
    shots: list[SceneSegment],
    similarity_threshold: float,
    max_group_shots: int,
    time_window: float,
) -> list[list[int]]:
    """Constrained UPGMA (average linkage) agglomerative clustering.

    Average linkage is updated in closed form via Lance-Williams, so no distance
    is ever recomputed from members, and the candidate search is vectorised.
    That keeps a 136-shot project in the millisecond range instead of the
    seconds a naive recomputation would cost.
    """
    count = len(features)
    scales = _Scales(features)
    distances = np.zeros((count, count), dtype=np.float64)
    for i in range(count):
        for j in range(i + 1, count):
            value = _feature_distance(features[i], features[j], scales)
            distances[i, j] = distances[j, i] = value

    members: list[list[int]] = [[i] for i in range(count)]
    sizes = np.ones(count, dtype=np.float64)
    starts = np.asarray([shot.start_time for shot in shots], dtype=np.float64)
    ends = np.asarray([shot.end_time for shot in shots], dtype=np.float64)
    active = np.ones(count, dtype=bool)
    linkage = distances.copy()
    np.fill_diagonal(linkage, np.inf)

    while int(active.sum()) > 1:
        index = np.flatnonzero(active)
        block = linkage[np.ix_(index, index)]
        # Time gap between cluster spans; 0 when the spans overlap.
        gap = np.maximum(
            starts[index][None, :] - ends[index][:, None],
            starts[index][:, None] - ends[index][None, :],
        )
        np.fill_diagonal(gap, 0.0)
        combined = sizes[index][:, None] + sizes[index][None, :]
        allowed = (
            (block <= similarity_threshold)
            & (gap <= time_window)
            & (combined <= max_group_shots)
        )
        np.fill_diagonal(allowed, False)
        if not allowed.any():
            break
        candidate = np.where(allowed, block, np.inf)
        # argmin on a C-ordered array resolves ties toward the lowest indices,
        # which is what keeps the whole grouping deterministic.
        flat = int(np.argmin(candidate))
        a_local, b_local = divmod(flat, len(index))
        a, b = int(index[a_local]), int(index[b_local])
        if a > b:
            a, b = b, a

        size_a, size_b = sizes[a], sizes[b]
        total = size_a + size_b
        # Lance-Williams update for UPGMA.
        linkage[a, :] = (size_a * linkage[a, :] + size_b * linkage[b, :]) / total
        linkage[:, a] = linkage[a, :]
        linkage[a, a] = np.inf
        linkage[b, :] = np.inf
        linkage[:, b] = np.inf

        members[a] = sorted(members[a] + members[b])
        members[b] = []
        sizes[a] = total
        starts[a] = min(starts[a], starts[b])
        ends[a] = max(ends[a], ends[b])
        active[b] = False

    partitions = [members[i] for i in np.flatnonzero(active)]
    partitions.sort(key=min)
    return partitions


def _split_incoherent_group(
    members: list[int], features: list[ShotFeature]
) -> list[list[int]]:
    """Split a group whose internal colour spread stayed too wide.

    The grouper checking its own output is what turns "14 white-balance jumps
    happened inside a group" from a QC observation into something the grouping
    stage can act on before the plan is ever built.
    """
    if len(members) < 4:
        return [members]
    mired = np.asarray([features[i].mired for i in members])
    luminance = np.asarray([features[i].log_luminance for i in members])
    mired_spread = float(np.ptp(mired))
    luminance_spread = float(np.ptp(luminance))
    if mired_spread <= MAX_GROUP_MIRED_SPREAD and luminance_spread <= MAX_GROUP_LUMINANCE_STOPS:
        return [members]
    # Split on whichever axis is the more violated, at its median.
    if mired_spread / max(MAX_GROUP_MIRED_SPREAD, 1e-6) >= luminance_spread / max(MAX_GROUP_LUMINANCE_STOPS, 1e-6):
        values, pivot = mired, float(np.median(mired))
    else:
        values, pivot = luminance, float(np.median(luminance))
    low = [member for member, value in zip(members, values) if value <= pivot]
    high = [member for member, value in zip(members, values) if value > pivot]
    if not low or not high:
        return [members]
    return [low, high]


def _hero_shot_index(members: list[int], features: list[ShotFeature], shots: list[SceneSegment]) -> int:
    """Pick the shot that best represents the group.

    The hero is the reference every other shot in the group is matched against,
    so statistical centrality outranks duration. V1 scored mostly on length and
    position, which happily nominated a long shot sitting at the edge of the
    group's colour distribution.
    """
    subset = [features[i] for i in members]
    scales = _Scales(subset)
    centre = ShotFeature.centroid(subset)
    best_member, best_score = members[0], -1e9
    for member in members:
        feature = features[member]
        shot = shots[member]
        clipping = 0.0
        if shot.analysis is not None:
            clipping = (
                shot.analysis.clipping.black_clipping_ratio
                + shot.analysis.clipping.highlight_clipping_ratio
            )
        score = (
            -2.0 * _feature_distance(feature, centre, scales)
            + 0.5 * min(shot.duration, 8.0) / 8.0
            - 1.0 * clipping
            + (0.3 if feature.face_ratio > 0.01 else 0.0)
        )
        if score > best_score + 1e-12:
            best_member, best_score = member, score
    return best_member


def group_diagnostics(
    members: list[int], features: list[ShotFeature]
) -> dict:
    mired = np.asarray([features[i].mired for i in members])
    luminance = np.asarray([features[i].log_luminance for i in members])
    return {
        "shot_count": len(members),
        "mired_spread": round(float(np.ptp(mired)), 3),
        "median_cct_kelvin": round(1e6 / max(float(np.median(mired)), 1e-6), 1),
        "luminance_spread_stops": round(float(np.ptp(luminance)), 4),
        "coherent": bool(
            float(np.ptp(mired)) <= MAX_GROUP_MIRED_SPREAD
            and float(np.ptp(luminance)) <= MAX_GROUP_LUMINANCE_STOPS
        ),
    }


def suggest_scene_groups(
    analysis: SceneAnalysisResult,
    similarity_threshold: float = 0.48,
    max_group_shots: int = 24,
    time_window: float = DEFAULT_TIME_WINDOW_SECONDS,
) -> list[SceneGroup]:
    """Group shots by lighting and colour coherence rather than adjacency.

    ``max_group_shots`` defaults higher than V1's 12 because it is now only a
    performance guard, not a grouping criterion.
    """
    shots = analysis.scenes
    if not shots:
        return []
    if len(shots) == 1:
        return [
            SceneGroup(
                scene_group_id="group_0001",
                shot_ids=[f"shot_{shots[0].index + 1:04d}"],
                hero_shot_id=f"shot_{shots[0].index + 1:04d}",
                label="场景组 1",
            )
        ]

    features = [ShotFeature(shot) for shot in shots]
    partitions = _cluster_shots(features, shots, similarity_threshold, max_group_shots, time_window)

    refined: list[list[int]] = []
    for members in partitions:
        refined.extend(_split_incoherent_group(members, features))
    refined.sort(key=lambda members: min(members))

    groups: list[SceneGroup] = []
    for order, members in enumerate(refined):
        hero = _hero_shot_index(members, features, shots)
        diagnostics = group_diagnostics(members, features)
        groups.append(
            SceneGroup(
                scene_group_id=f"group_{order + 1:04d}",
                shot_ids=[f"shot_{shots[i].index + 1:04d}" for i in sorted(members)],
                hero_shot_id=f"shot_{shots[hero].index + 1:04d}",
                label=f"场景组 {order + 1}",
                diagnostics=diagnostics,
            )
        )
    return groups

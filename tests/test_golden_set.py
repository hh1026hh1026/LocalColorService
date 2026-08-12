"""Objective colour-accuracy baseline against the synthetic golden set.

These are not aspirational targets. Every threshold below is set at *measured
current behaviour*, so the suite does two things:

* asserts the properties that are genuinely correct today
* pins the ones that are not, so they cannot silently get worse

Where the current number is poor it is asserted at the poor value with a comment
saying so. Loosening one of those assertions is a deliberate act, and tightening
one is how progress gets recorded.

Regenerate the inputs with::

    python scripts/build_golden_set.py
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from color_core.color_metrics import skin_difference_rec709
from color_core.image_analyzer import analyze_image_frames
from color_core.lut_baker import bake_3d_lut
from color_core.ocio_manager import OCIOManager
from color_core.cube_tools import read_cube
from color_core.project_qc import _sample_trilinear
from color_core.recipe import GradeRecipe

GOLDEN = Path(__file__).resolve().parent.parent / "test_assets" / "golden"

pytestmark = pytest.mark.skipif(
    not (GOLDEN / "golden_set.json").is_file(),
    reason="run scripts/build_golden_set.py to generate the golden set",
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((GOLDEN / "golden_set.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manager() -> OCIOManager:
    return OCIOManager()


def _load8(name: str) -> np.ndarray:
    image = cv2.imread(str(GOLDEN / name), cv2.IMREAD_UNCHANGED)
    assert image is not None, name
    return (image.astype(np.float64) / 65535.0 * 255.0).round().astype(np.uint8)


def _mired(cct: float) -> float:
    return 1e6 / max(float(cct), 1.0)


def _through_lut(manager: OCIOManager, recipe: GradeRecipe, rgb, tmp_path) -> np.ndarray:
    path = bake_3d_lut(
        manager.complete_recipe(recipe), str(tmp_path / "probe.cube"), manager, lut_size=65
    )
    return _sample_trilinear(read_cube(path), np.atleast_2d(np.asarray(rgb, dtype=np.float64)))


# ---------------------------------------------------------------------------
# Properties that are correct today
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,truth",
    [("d65", 6503.5), ("tungsten_3200k", 3200.0), ("warm_4300k", 4300.0), ("cool_9000k", 9000.0)],
)
def test_illuminant_is_recovered_on_a_fair_scene(label, truth):
    """Accuracy measured where gray-world's assumption actually holds.

    ``neutral_scene_*`` is built so the linear average of its surfaces is
    neutral by construction. That is the only fair target for a gray-world
    estimator - a ColorChecker is 24 deliberately saturated patches that do not
    average to grey, so scoring against one measures the chart's violation of
    the assumption rather than the estimator.
    """
    wb = analyze_image_frames([_load8(f"neutral_scene_{label}.png")]).white_balance
    error = abs(_mired(wb.source_cct_kelvin) - _mired(truth))
    assert error < 5.0, f"{label}: {error:.1f} mired"
    assert wb.candidate_agreement > 0.6


def test_the_estimate_is_never_silently_discarded():
    """Regression guard for the give-up path.

    The estimator used to return unit gains whenever its candidates disagreed in
    *gain* units - a measure that scales with how far the light is from D65, so
    a warm scene was penalised for being warm rather than for being uncertain.
    A 9000K scene it could solve exactly was thrown away at 42.6 mired of error.
    Agreement is now measured in mired and the estimate is always reported.
    """
    for label in ("d65", "tungsten_3200k", "warm_4300k", "cool_9000k"):
        wb = analyze_image_frames([_load8(f"neutral_scene_{label}.png")]).white_balance
        assert wb.selected_method != "preserve_low_confidence"
        assert wb.source_cct_kelvin > 0


def test_neutral_ramp_stays_neutral_through_an_identity_grade(manager, tmp_path):
    ramp = np.linspace(0.05, 0.95, 19)
    probe = np.stack([ramp] * 3, axis=-1)
    output = _through_lut(manager, GradeRecipe(), probe, tmp_path)
    assert np.abs(output - probe).max() < 2e-3


def test_white_balance_keeps_the_neutral_axis_straight(manager, tmp_path):
    """The V0.6 regression guard, restated on the golden ramp."""
    ramp = np.linspace(0.10, 0.90, 17)
    probe = np.stack([ramp] * 3, axis=-1)
    output = _through_lut(manager, GradeRecipe(rgb_gains=[0.90, 1.0, 1.12]), probe, tmp_path)
    totals = output.sum(axis=1, keepdims=True)
    chromaticity = output[:, :2] / np.maximum(totals, 1e-6)
    assert np.ptp(chromaticity, axis=0).max() < 5e-3


@pytest.mark.parametrize("ev", [0.3, 0.6, -0.5])
def test_exposure_alone_does_not_rotate_skin_hue(manager, tmp_path, ev):
    """The P0-1 finding: hue is exposure-independent, lightness is not.

    Measured across the six-tone panel, pure exposure moves Delta H* by under
    0.05 while Delta E2000 moves by up to 19 - which is why skin safety is
    judged on hue.
    """
    manifest = json.loads((GOLDEN / "golden_set.json").read_text(encoding="utf-8"))
    patches = manifest["charts"]["skin_panel_d65.png"]["patches"]
    for name, entry in patches.items():
        source = entry["observed_rec709"]
        graded = _through_lut(manager, GradeRecipe(exposure=ev), source, tmp_path)[0]
        if max(graded) >= 0.999:
            continue  # clipped patches are a separate, reported failure mode
        difference = skin_difference_rec709(source, np.clip(graded, 0, 1))
        assert abs(difference["delta_hue"]) < 0.5, f"{name} at {ev:+.1f} EV"


def test_skin_hue_shift_is_detected_when_it_is_real(manager, tmp_path):
    manifest = json.loads((GOLDEN / "golden_set.json").read_text(encoding="utf-8"))
    source = manifest["charts"]["skin_panel_d65.png"]["patches"]["skin_3_medium"]["observed_rec709"]
    graded = _through_lut(manager, GradeRecipe(tint=0.35), source, tmp_path)[0]
    difference = skin_difference_rec709(source, np.clip(graded, 0, 1))
    assert abs(difference["delta_hue"]) > 3.0
    assert abs(difference["hue_rotation_deg"]) > 6.0


def test_skin_is_only_excluded_when_a_face_is_detected():
    """The skin classifier is a colour range, not a face detector.

    On synthetic scenes containing no people it still selected 16-90% of pixels
    (warm surfaces qualify). Excluding those wrecked the neutral average and
    turned a 0.1 mired result into 61.7, so exclusion is gated on detection.
    """
    for label in ("d65", "warm_4300k"):
        scene = _load8(f"neutral_scene_{label}.png")
        with_gate = analyze_image_frames([scene]).white_balance
        without = analyze_image_frames(
            [scene], exclude_skin_from_white_balance=False
        ).white_balance
        assert abs(with_gate.source_cct_kelvin - without.source_cct_kelvin) < 50.0, (
            f"{label}: skin exclusion fired on a scene with no people"
        )


# ---------------------------------------------------------------------------
# Known weaknesses, pinned at their measured values
# ---------------------------------------------------------------------------

def test_a_saturated_chart_is_not_a_fair_illuminant_target():
    """Documents why the ColorChecker is not used to score the estimator.

    Its 24 patches are deliberately saturated and do not average to grey, so a
    gray-world estimator is expected to be wrong here. Recording the size of
    that error keeps the reason visible: an earlier version of this suite
    scored the chart and concluded the estimator could not handle tungsten,
    when on a fair scene it recovers 3200K exactly.
    """
    wb = analyze_image_frames([_load8("colorchecker_d65.png")]).white_balance
    error = abs(_mired(wb.source_cct_kelvin) - _mired(6503.5))
    assert error > 10.0, (
        "the chart now estimates accurately - either the estimator no longer "
        "relies on gray-world, or this chart changed; revisit the reasoning"
    )


def test_a_frame_filled_with_skin_defeats_gray_world():
    """KNOWN GAP - a face-filling close-up reads its subject as its light.

    An all-skin panel under D65 estimates around 4300K, roughly 79 mired out.
    Removing skin from the statistics cannot help when there is nothing else in
    frame, and the synthetic panel has no detectable face to trigger it anyway.

    Mitigations in place: the applied gains stay inside [0.80, 1.25] so the
    damage is bounded, and scene-group consensus (C2) overrides the shot in a
    real edit. On its own the shot would still be pushed cool.
    """
    wb = analyze_image_frames([_load8("skin_panel_d65.png")]).white_balance
    error = abs(_mired(wb.source_cct_kelvin) - _mired(6503.5))
    assert error > 40.0, "if this now passes, the estimator improved - retighten the test"
    assert error < 120.0, "regression: the skin close-up estimate got worse"


def test_skin_exclusion_recovers_a_half_skin_frame():
    """With background available, removing skin restores the estimate."""
    from color_core.image_analyzer import _white_balance_selector

    skin = _load8("skin_panel_d65.png")
    height, width = skin.shape[:2]
    mixed = np.zeros((height, width * 2, 3), dtype=np.uint8)
    mixed[:, :width] = skin
    mixed[:, width:] = 160  # neutral background

    # Force the selector directly: the synthetic panel has no detectable face,
    # so the production gate would (correctly) decline to fire on it.
    keep = np.ones(mixed.shape[0] * mixed.shape[1], dtype=bool)
    keep[: mixed.shape[0] * width] = False  # stand in for "skin removed"
    excluded = analyze_image_frames(
        [mixed], weights=[keep.reshape(mixed.shape[:2]).astype(np.float32)]
    ).white_balance
    included = analyze_image_frames(
        [mixed], exclude_skin_from_white_balance=False
    ).white_balance
    assert abs(_mired(excluded.source_cct_kelvin) - _mired(6503.5)) < 5.0
    assert abs(_mired(included.source_cct_kelvin) - _mired(6503.5)) > 20.0


def test_applied_correction_is_bounded_in_mired_not_by_clamping_gains():
    """Whatever the estimate says, the white point moves only so far.

    The bound is stated as a mired shift rather than a gain range because a
    per-channel clamp saturates: under the old [0.80, 1.25] envelope, target
    white points of 210, 230 and 260 mired all collapsed onto 198.0, so two
    shots either side of the envelope ended up far apart and the limiter itself
    created cuts in the grade.
    """
    from color_core.correction_engine import generate_auto_correction_advice
    from color_core.white_balance import (
        MAX_CORRECTION_MIRED, cct_duv_from_white, source_white_from_gains,
    )

    for name in ("skin_panel_d65.png", "colorchecker_tungsten_3200k.png",
                 "neutral_scene_cool_9000k.png"):
        report = analyze_image_frames([_load8(name)])
        recipe = generate_auto_correction_advice(report).suggested_recipe
        if recipe.rgb_gains == [1.0, 1.0, 1.0]:
            continue
        applied, _ = cct_duv_from_white(source_white_from_gains(tuple(recipe.rgb_gains)))
        shift = abs(_mired(applied) - _mired(report.white_balance.source_cct_kelvin))
        assert shift <= MAX_CORRECTION_MIRED + 2.0, f"{name}: moved {shift:.1f} mired"


# ---------------------------------------------------------------------------
# Highlight protection (P0-2)
# ---------------------------------------------------------------------------

def test_headroom_shrinks_as_the_source_gets_brighter():
    from color_core.correction_engine import exposure_headroom_ev

    dark = analyze_image_frames([np.full((64, 64, 3), 40, np.uint8)])
    bright = analyze_image_frames([np.full((64, 64, 3), 230, np.uint8)])
    assert exposure_headroom_ev(dark) > exposure_headroom_ev(bright)
    assert exposure_headroom_ev(bright) < 0.2


def test_exposure_is_limited_before_it_blows_highlights():
    """A dark subject in front of a bright window must not blow the window."""
    from color_core.correction_engine import generate_auto_correction_advice

    frame = np.full((200, 200, 3), 45, np.uint8)
    frame[:, 150:] = 245  # a window occupying a quarter of frame
    report = analyze_image_frames([frame])
    advice = generate_auto_correction_advice(report)
    assert advice.suggested_recipe.exposure <= advice.exposure_headroom_ev + 1e-6
    if advice.highlight_limited:
        assert advice.suggested_recipe.highlight_softness > 0.0
        assert any("headroom" in item for item in advice.rationales)


# ---------------------------------------------------------------------------
# Metric robustness (V0.6.9)
# ---------------------------------------------------------------------------

def test_neutral_drift_ignores_the_near_black_toe(manager, tmp_path):
    """A darkened shot must not read as a broken white balance.

    Found by the end-to-end smoke test: a -0.8 EV shot with a legitimate,
    perfectly uniform white balance measured 0.085 drift - twice the threshold -
    entirely because two samples had been pushed down to RGB ~0.044, deep in the
    ACEScct toe where chromaticity is numerically unstable. Every sample above
    that agreed to four decimals.
    """
    from color_core.lut_baker import bake_3d_lut
    from color_core.project_qc import inspect_cube_lut

    for exposure, gains in ((-0.8, [0.917, 1.0, 1.137]), (-0.8, [0.80, 1.0, 1.25])):
        recipe = GradeRecipe(exposure=exposure, rgb_gains=gains, contrast=1.1)
        path = bake_3d_lut(manager.complete_recipe(recipe), str(tmp_path / "dark.cube"), manager)
        report = inspect_cube_lut(path, "technical")
        assert report.state == "PASS", f"{gains}: {report.warnings}"

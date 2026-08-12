"""
Unit Test for Reinhard Color Transfer Algorithm & Plugin Provider in V0.1.3.
"""

import numpy as np
from color_core.reinhard_transfer import reinhard_color_transfer, generate_recipe_from_reference
from color_core.plugin_interface import ReinhardColorTransferProvider, ColorSuggestion


def test_reinhard_color_transfer():
    src_bgr = np.full((100, 100, 3), 128, dtype=np.uint8)
    ref_bgr = np.full((100, 100, 3), (50, 120, 220), dtype=np.uint8)

    transferred = reinhard_color_transfer(src_bgr, ref_bgr, blend_factor=1.0)

    assert transferred.shape == (100, 100, 3)
    assert np.mean(transferred[:, :, 2]) > np.mean(src_bgr[:, :, 2])

    recipe = generate_recipe_from_reference(src_bgr, ref_bgr)
    assert recipe.filmic_s_curve is True
    assert recipe.confidence > 0.5
    assert len(recipe.rationales) > 0


def test_reinhard_plugin_provider():
    provider = ReinhardColorTransferProvider()
    assert provider.provider_name == "reinhard_transfer"

    src_bgr = np.full((50, 50, 3), 100, dtype=np.uint8)
    ref_bgr = np.full((50, 50, 3), 200, dtype=np.uint8)

    suggestion = provider.analyze_and_suggest(
        analysis_report=None,
        context={"source_frame": src_bgr, "reference_frame": ref_bgr}
    )

    assert isinstance(suggestion, ColorSuggestion)
    assert suggestion.provider_name == "reinhard_transfer"
    assert suggestion.recommended is True

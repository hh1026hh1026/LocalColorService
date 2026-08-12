"""
Unit Test for Multi-Provider Registry & Algorithm Selection System in V0.1.3.
"""

import numpy as np
from color_core.plugin_interface import (
    suggestion_registry,
    TraditionalAutoCorrectionProvider,
    ReinhardColorTransferProvider,
    CLAHEAdaptiveProvider,
    AdaptiveLUTDeterministicProvider
)
from color_core.image_analyzer import analyze_image_frames


def test_provider_registry_and_providers():
    providers = suggestion_registry.list_providers()
    assert len(providers) == 4

    # Test traditional_rule
    p_trad = suggestion_registry.get("traditional_rule")
    assert isinstance(p_trad, TraditionalAutoCorrectionProvider)

    # Test clahe_adaptive
    p_clahe = suggestion_registry.get("clahe_adaptive")
    assert isinstance(p_clahe, CLAHEAdaptiveProvider)

    # Deterministic provider is named accurately; real AdaInt has a dedicated LUT endpoint.
    p_adaptive = suggestion_registry.get("adaptive_lut_deterministic")
    assert isinstance(p_adaptive, AdaptiveLUTDeterministicProvider)

    # Evaluate analysis with CLAHE
    dummy_img = np.full((100, 100, 3), 128, dtype=np.uint8)
    report = analyze_image_frames([dummy_img])
    sugg = p_clahe.analyze_and_suggest(report)
    assert sugg.recipe.filmic_s_curve is True
    assert sugg.recipe.contrast > 1.0

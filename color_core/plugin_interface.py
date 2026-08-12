"""
Color Suggestion Provider Plugin Architecture Interface & Registry for Local Color Service V0.1.3.
Provides a unified strategy registry for rule-based, Reinhard transfer, CLAHE, and AdaInt neural algorithms.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import cv2
import numpy as np
from pydantic import BaseModel, Field
from color_core.image_analyzer import ImageAnalysisReport
from color_core.recipe import GradeRecipe
from color_core.correction_engine import generate_auto_correction_advice
from color_core.reinhard_transfer import generate_recipe_from_reference


def _bounded_auto_recipe(recipe: GradeRecipe, provider_name: str) -> GradeRecipe:
    """Apply one conservative safety envelope to every automatic provider."""
    data = recipe.model_dump()
    data.update(
        exposure=max(-1.0, min(1.0, float(recipe.exposure))),
        contrast=max(0.85, min(1.20, float(recipe.contrast))),
        saturation=max(0.85, min(1.15, float(recipe.saturation))),
        temperature=max(-20.0, min(20.0, float(recipe.temperature))),
        # Linear-light gains since V0.6.2; the display-domain [0.88, 1.12]
        # envelope this used to carry would now clip real corrections.
        rgb_gains=[max(0.80, min(1.25, float(value))) for value in recipe.rgb_gains],
        confidence=max(0.0, min(0.95, float(recipe.confidence))),
    )
    bounds = dict(recipe.safety_bounds)
    bounds.update({
        "exposure": "[-1.0, +1.0] EV",
        "contrast": "[0.85, 1.20]",
        "saturation": "[0.85, 1.15]",
        "temperature": "[-20, +20]",
        "rgb_gains": "[0.80, 1.25] linear",
        "provider": provider_name,
    })
    data["safety_bounds"] = bounds
    data["applied_safety_bounds"] = bounds
    return GradeRecipe.model_validate(data)


class ColorSuggestion(BaseModel):
    provider_name: str
    recommended: bool = True
    applied: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationales: list[str] = Field(default_factory=list)
    recipe: GradeRecipe


class ColorSuggestionProvider(ABC):
    """
    Abstract Base Class for all color auto-correction and ML/AI suggestion providers.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    def analyze_and_suggest(
        self,
        analysis_report: ImageAnalysisReport,
        context: Optional[Dict[str, Any]] = None
    ) -> ColorSuggestion:
        """
        Analyzes image statistics / context and returns a standardized ColorSuggestion.
        """
        pass


class TraditionalAutoCorrectionProvider(ColorSuggestionProvider):
    """
    Standard Rule-Based & Statistical Color Suggestion Provider (Default Engine).
    """

    @property
    def provider_name(self) -> str:
        return "traditional_rule"

    def analyze_and_suggest(
        self,
        analysis_report: ImageAnalysisReport,
        context: Optional[Dict[str, Any]] = None
    ) -> ColorSuggestion:
        source_hash = context.get("source_hash", "") if context else ""
        target_lut_size = context.get("lut_size", 33) if context else 33

        advice = generate_auto_correction_advice(
            report=analysis_report,
            source_hash=source_hash,
            target_lut_size=target_lut_size
        )

        return ColorSuggestion(
            provider_name=self.provider_name,
            recommended=True,
            applied=False,
            confidence=advice.confidence,
            rationales=advice.rationales,
            recipe=_bounded_auto_recipe(advice.suggested_recipe, self.provider_name)
        )


class ReinhardColorTransferProvider(ColorSuggestionProvider):
    """
    Reinhard L*a*b* Statistical Reference Image Color Transfer Provider.
    Falls back gracefully to cinematic reference stats if custom reference image is omitted.
    """

    @property
    def provider_name(self) -> str:
        return "reinhard_transfer"

    def analyze_and_suggest(
        self,
        analysis_report: ImageAnalysisReport,
        context: Optional[Dict[str, Any]] = None
    ) -> ColorSuggestion:
        source_bgr = None
        reference_bgr = None

        if context:
            source_bgr = context.get("source_frame")
            reference_bgr = context.get("reference_frame")

        if source_bgr is None or reference_bgr is None:
            fallback = TraditionalAutoCorrectionProvider().analyze_and_suggest(analysis_report, context)
            recipe = fallback.recipe.model_copy(deep=True)
            recipe.rationales.insert(0, "Reinhard reference was not supplied; safely fell back to traditional analysis.")
            confidence = min(fallback.confidence, 0.70)
            recipe.confidence = confidence
        else:
            recipe = generate_recipe_from_reference(source_bgr, reference_bgr)
            recipe.rationales.append("Reinhard Transfer: matched the supplied reference frame conservatively.")
            confidence = recipe.confidence
        recipe = _bounded_auto_recipe(recipe, self.provider_name)

        return ColorSuggestion(
            provider_name=self.provider_name,
            recommended=True,
            applied=False,
            confidence=confidence,
            rationales=recipe.rationales,
            recipe=recipe
        )


class CLAHEAdaptiveProvider(ColorSuggestionProvider):
    """
    Contrast Limited Adaptive Histogram Equalization (CLAHE) Color Suggestion Provider.
    """

    @property
    def provider_name(self) -> str:
        return "clahe_adaptive"

    def analyze_and_suggest(
        self,
        analysis_report: ImageAnalysisReport,
        context: Optional[Dict[str, Any]] = None
    ) -> ColorSuggestion:
        mean_lum = analysis_report.luminance.mean if analysis_report else 0.5
        contrast_range = analysis_report.contrast.range_p05_p95 if analysis_report else 0.8

        target_contrast = 1.15 if contrast_range < 0.7 else 1.05
        target_exp = 0.15 if mean_lum < 0.4 else (0.0 if mean_lum <= 0.65 else -0.1)

        recipe = GradeRecipe(
            exposure=target_exp,
            contrast=target_contrast,
            saturation=1.12,
            filmic_s_curve=True,
            confidence=0.86,
            rationales=[
                f"CLAHE Local Contrast Enhancement: Target contrast set to {target_contrast:.2f}",
                f"Adaptive luminance correction: Exposure set to {target_exp:+.2f} EV",
                "Enhanced color vibrancy (+12% saturation)"
            ]
        )

        return ColorSuggestion(
            provider_name=self.provider_name,
            recommended=True,
            applied=False,
            confidence=0.86,
            rationales=recipe.rationales,
            recipe=_bounded_auto_recipe(recipe, self.provider_name)
        )


class AdaptiveLUTDeterministicProvider(ColorSuggestionProvider):
    """
    Lightweight deterministic LUT-style suggestion provider.

    This is deliberately separate from the real AdaInt checkpoint endpoint.
    """

    @property
    def provider_name(self) -> str:
        return "adaptive_lut_deterministic"

    def analyze_and_suggest(
        self,
        analysis_report: ImageAnalysisReport,
        context: Optional[Dict[str, Any]] = None
    ) -> ColorSuggestion:
        mean_lum = analysis_report.luminance.mean if analysis_report else 0.5
        target_exp = round(max(-0.4, min(0.4, (0.52 - mean_lum) * 0.7)), 2)

        recipe = GradeRecipe(
            exposure=target_exp,
            contrast=1.04,
            saturation=1.15,
            temperature=0.0,
            rgb_gains=[1.0, 1.0, 1.0],
            filmic_s_curve=True,
            confidence=0.84,
            rationales=[
                "Deterministic adaptive LUT recipe generated from image statistics",
                f"Statistical exposure prediction: {target_exp:+.2f} EV with filmic S-Curve compression",
                "Calibrated chroma retention (+15% control value) without an artificial color cast"
            ]
        )

        return ColorSuggestion(
            provider_name=self.provider_name,
            recommended=True,
            applied=False,
            confidence=0.84,
            rationales=recipe.rationales,
            recipe=_bounded_auto_recipe(recipe, self.provider_name)
        )


class ColorSuggestionRegistry:
    """
    Singleton Registry for managing and retrieving ColorSuggestionProviders.
    """

    def __init__(self):
        self._providers: Dict[str, ColorSuggestionProvider] = {}
        self.register(TraditionalAutoCorrectionProvider())
        self.register(ReinhardColorTransferProvider())
        self.register(CLAHEAdaptiveProvider())
        self.register(AdaptiveLUTDeterministicProvider())

    def register(self, provider: ColorSuggestionProvider) -> None:
        self._providers[provider.provider_name] = provider

    def get(self, name: str) -> ColorSuggestionProvider:
        if name not in self._providers:
            return self._providers["traditional_rule"]
        return self._providers[name]

    def list_providers(self) -> List[Dict[str, str]]:
        return [
            {"id": "traditional_rule", "name": "传统规则 + 胶片 S 曲线 (Traditional Rule-based)"},
            {"id": "reinhard_transfer", "name": "参考风格图色彩迁移 (Reinhard Color Transfer)"},
            {"id": "clahe_adaptive", "name": "CLAHE 自适应局部对比度 (CLAHE Adaptive)"},
            {"id": "adaptive_lut_deterministic", "name": "确定性自适应 LUT 配方 (Adaptive LUT Deterministic)"}
        ]


# Import compatibility for V0.1.3 code. The provider itself no longer claims to
# be neural and is not registered under the old API id.
AdaIntNeuralProvider = AdaptiveLUTDeterministicProvider


suggestion_registry = ColorSuggestionRegistry()

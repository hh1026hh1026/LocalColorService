import pytest
from pydantic import ValidationError

from color_core.correction_engine import generate_auto_correction_advice
from color_core.image_analyzer import analyze_image_frames
from color_core.recipe import GradeRecipe


def test_recipe_validation_and_auto_bounds(neutral_bgr):
    advice = generate_auto_correction_advice(analyze_image_frames([neutral_bgr]))
    recipe = advice.suggested_recipe
    assert -1.0 <= recipe.exposure <= 1.0
    assert 0.85 <= recipe.contrast <= 1.20
    assert 0.85 <= recipe.saturation <= 1.15
    assert 0.0 <= advice.confidence <= 1.0
    assert recipe.safety_bounds
    with pytest.raises(ValidationError):
        GradeRecipe(lut_size=32)

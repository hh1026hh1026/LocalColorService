import PyOpenColorIO as ocio

from color_core.ocio_manager import OCIOManager
from color_core.recipe import GradeRecipe


def test_aces_studio_config_and_group_transform():
    manager = OCIOManager()
    recipe = manager.complete_recipe(GradeRecipe())
    assert recipe.working_color_space == "ACEScct"
    assert recipe.input_color_space in manager.list_color_spaces()
    assert recipe.output_color_space in manager.list_color_spaces()
    group = manager.build_grade_group(recipe)
    assert isinstance(group, ocio.GroupTransform)
    assert len(list(group)) >= 4

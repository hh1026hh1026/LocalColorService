"""ASC-CDL and CLF interchange exports."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import PyOpenColorIO as ocio

from color_core.ocio_manager import OCIOManager
from color_core.project_recipe import ProjectGradeRecipe
from color_core.recipe import GradeRecipe
from color_core.white_balance import white_balance_matrix


def _cdl_values(recipe: GradeRecipe) -> tuple[list[float], list[float], list[float], float]:
    """Best diagonal approximation of the grade for ASC-CDL interchange.

    Since V0.6 the white balance is a Bradford chromatic adaptation, i.e. a full
    3x3 matrix with non-trivial off-diagonal terms. ASC-CDL's SOP node is purely
    per-channel and cannot represent that exactly, so the slope carries the
    von Kries diagonal of the real matrix (in Rec.709 linear primaries) rather
    than the raw encoded-domain rgb_gains. Direction and magnitude then match
    what the renderer actually does; the cross-channel residual does not survive.

    Use export_clf() when an exact transform is required - it bakes the real
    GroupTransform and is lossless.
    """
    matrix = white_balance_matrix(
        tuple(recipe.rgb_gains), recipe.temperature, recipe.tint,
        recipe.strength, basis_name="ITU-R BT.709",
    )
    diagonal = [float(matrix[i][i]) for i in range(3)]
    slope = [diagonal[i] * recipe.gain[i] for i in range(3)]
    offset = list(recipe.lift)
    power = [1.0 / max(0.01, value) for value in recipe.gamma]
    return slope, offset, power, recipe.saturation


def export_cc(recipe: GradeRecipe, output_path: str, correction_id: str = "grade") -> str:
    slope, offset, power, saturation = _cdl_values(recipe)
    root = ET.Element("ColorCorrection", id=correction_id)
    sop = ET.SubElement(root, "SOPNode")
    ET.SubElement(sop, "Slope").text = " ".join(f"{v:.9g}" for v in slope)
    ET.SubElement(sop, "Offset").text = " ".join(f"{v:.9g}" for v in offset)
    ET.SubElement(sop, "Power").text = " ".join(f"{v:.9g}" for v in power)
    sat = ET.SubElement(root, "SatNode")
    ET.SubElement(sat, "Saturation").text = f"{saturation:.9g}"
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return str(path)


def export_ccc(project: ProjectGradeRecipe, output_path: str) -> str:
    root = ET.Element("ColorCorrectionCollection")
    for shot in project.shots:
        temp = Path(output_path).with_suffix(f".{shot.scene_id}.cc")
        export_cc(project.effective_recipe(shot), str(temp), shot.scene_id)
        root.append(ET.parse(temp).getroot())
        temp.unlink(missing_ok=True)
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return str(path)


def export_clf(recipe: GradeRecipe, output_path: str, manager: OCIOManager) -> str:
    complete = manager.complete_recipe(recipe)
    config = ocio.Config.CreateFromBuiltinConfig(manager.config_id) if not Path(manager.config_id).is_file() else ocio.Config.CreateFromFile(manager.config_id)
    target_name = "LocalColorService CLF Output"
    target = ocio.ColorSpace(referenceSpace=ocio.REFERENCE_SPACE_SCENE)
    target.setName(target_name)
    target.setTransform(manager.build_grade_group(complete), ocio.COLORSPACE_DIR_FROM_REFERENCE)
    config.addColorSpace(target)
    baker = ocio.Baker()
    baker.setConfig(config)
    baker.setFormat("Academy/ASC Common LUT Format")
    baker.setInputSpace(complete.input_color_space)
    baker.setTargetSpace(target_name)
    baker.setCubeSize(complete.lut_size)
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baker.bake(), encoding="utf-8", newline="\n")
    return str(path)

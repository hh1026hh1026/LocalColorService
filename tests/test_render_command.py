from pathlib import Path
from color_core.renderer import build_render_command
from app.schemas.api_schemas import PreviewRequest
from color_core.recipe import GradeRecipe

ASSETS = Path(__file__).parents[1] / "test_assets"


def test_render_command_contract(tmp_path):
    lut = tmp_path / "a.cube"
    lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8, encoding="utf-8")
    command = build_render_command(str(ASSETS / "neutral_sample.mp4"), str(lut), str(tmp_path / "out.mp4"), target_height=720, video_codec="h264_nvenc")
    joined = " ".join(command)
    assert "lut3d=" in joined and "interp=tetrahedral" in joined
    assert "scale=-2:720" in joined
    assert "h264_nvenc" in command and "copy" in command
    assert "-color_primaries" in command and "bt709" in command
    aac_command = build_render_command(str(ASSETS / "neutral_sample.mp4"), str(lut), str(tmp_path / "aac.mp4"), video_codec="libx264", audio_codec="aac")
    assert "libx264" in aac_command and "aac" in aac_command
    assert PreviewRequest(input_path=str(ASSETS / "neutral_sample.mp4"), recipe=GradeRecipe()).target_height == 720

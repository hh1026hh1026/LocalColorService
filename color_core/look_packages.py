"""Conservative, ACES-managed creative Look Packages for V0.2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from color_core.recipe import GradeRecipe


DESIGN_BASIS = [
    {
        "name": "ACES Look Transform 设计指南",
        "url": "https://docs.acescentral.com/system-components/look-transforms/specification/",
    },
    {
        "name": "ITU-R BT.709-6",
        "url": "https://www.itu.int/rec/R-REC-BT.709/en",
    },
    {
        "name": "EBU R 103 视频信号容差",
        "url": "https://tech.ebu.ch/publications/r103",
    },
]


def _look(
    english_name: str,
    name: str,
    suitable_for: list[str],
    characteristics: list[str],
    must_protect: list[str],
    defaults: dict[str, Any],
    technical_notes: str,
) -> dict[str, Any]:
    return {
        "display_name_en": english_name,
        "name": name,
        "description": "、".join(characteristics),
        "suitable_for": suitable_for,
        "core_characteristics": characteristics,
        "must_protect": must_protect,
        "technical_notes": technical_notes,
        "classification": "工程创意预设（非 ACES 官方 LMT）",
        "design_basis": deepcopy(DESIGN_BASIS),
        "defaults": defaults,
    }


LOOK_PACKAGES: dict[str, dict[str, Any]] = {
    "neutral_broadcast": _look(
        "Neutral Broadcast", "自然广播", ["新闻", "访谈", "纪录片"],
        ["中性", "稳定", "肤色自然", "广播安全"], ["肤色", "白色", "合法电平"],
        {"contrast": 1.0, "saturation": 1.0, "highlight_softness": 0.12,
         "color_density": 0.0, "gamut_protection": 0.95, "skin_protection": 0.90},
        "以 BT.709/D65 中性呈现为目标，避免创意偏色，并为 EBU R103 电平检查保留余量。",
    ),
    "clean_commercial": _look(
        "Clean Commercial", "明亮商业", ["产品", "企业片", "教育"],
        ["明亮", "干净", "白色纯净", "适度清晰"], ["产品颜色", "白背景"],
        {"exposure": 0.08, "contrast": 1.06, "saturation": 1.04, "temperature": 1.5,
         "highlight_softness": 0.28, "color_density": 0.08,
         "gamut_protection": 0.90, "skin_protection": 0.85},
        "只做轻量曝光和对比提升；白色保护依赖先完成技术白平衡，锐度不写入 LUT。",
    ),
    "cinematic_soft_print": _look(
        "Cinematic Soft Print", "柔和电影印片", ["剧情", "宣传片"],
        ["柔和肩部", "厚实中间调", "轻微暖高光"], ["高光", "红色", "肤色"],
        {"contrast": 1.08, "saturation": 0.96, "temperature": 4.0,
         "highlight_softness": 0.65, "color_density": 0.25,
         "shadow_color": [-0.004, 0.0, 0.006],
         "gamut_protection": 0.95, "skin_protection": 0.90},
        "采用解析式软高光和中间调密度，不声称模拟某一种胶片或印片库存。",
    ),
    "restrained_teal_amber": _look(
        "Restrained Teal–Amber", "克制青橙", ["预告", "科技", "动作"],
        ["冷阴影", "暖肤色倾向", "低强度色彩分离"], ["肤色", "天空", "黑位"],
        {"exposure": -0.03, "contrast": 1.10, "saturation": 0.96, "temperature": 3.0,
         "highlight_softness": 0.45, "color_density": 0.12,
         "shadow_color": [-0.010, 0.002, 0.014],
         "gamut_protection": 1.0, "skin_protection": 0.95},
        "只向暗部加入小幅青蓝偏置并保留轻微整体暖度，避免把中性物和肤色整体染成橙色。",
    ),
    "warm_memory": _look(
        "Warm Memory", "暖调回忆", ["文旅", "人物", "回忆"],
        ["暖中高光", "轻抬暗部观感", "降低蓝绿存在感"], ["白色", "中性灰"],
        {"exposure": 0.06, "contrast": 0.96, "saturation": 0.88, "temperature": 8.0,
         "highlight_softness": 0.48, "color_density": 0.08,
         "shadow_color": [0.004, 0.0, -0.006],
         "gamut_protection": 0.95, "skin_protection": 0.95},
        "暖化幅度保持在全局色温的小范围内；中性白与灰应通过镜头技术校正先行锁定。",
    ),
    "cold_thriller": _look(
        "Cold Thriller", "冷峻悬疑", ["悬疑", "工业", "夜景"],
        ["冷阴影", "环境低饱和", "肤色相对中性"], ["人脸", "暗部细节"],
        {"exposure": -0.12, "contrast": 1.10, "saturation": 0.78, "temperature": -10.0,
         "highlight_softness": 0.38, "color_density": 0.10,
         "shadow_color": [-0.008, 0.002, 0.016],
         "gamut_protection": 0.95, "skin_protection": 0.95},
        "通过降环境饱和度和小幅冷阴影形成气氛；当前为全画面 Look，人脸保护仍需后续语义遮罩。",
    ),
    "sports_vivid": _look(
        "Sports Vivid", "赛事鲜明", ["足球", "篮球", "赛事"],
        ["清晰", "较高局部对比观感", "队服颜色鲜明"], ["草地", "肤色", "LED"],
        {"exposure": 0.08, "contrast": 1.10, "saturation": 1.12,
         "highlight_softness": 0.30, "color_density": 0.12,
         "gamut_protection": 1.0, "skin_protection": 0.90},
        "饱和度提升受控并配合最高色域保护权重；真正局部对比和锐化不属于 3D LUT。",
    ),
    "stage_mixed_light": _look(
        "Stage Mixed Light", "舞台混光", ["演出", "OPC", "舞台"],
        ["抑制洋红/绿色过冲", "保留彩灯氛围", "软化 LED 高光"], ["LED", "高饱和灯光", "肤色"],
        {"contrast": 1.02, "saturation": 0.92,
         "highlight_softness": 0.70, "color_density": 0.06,
         "gamut_protection": 1.0, "skin_protection": 1.0},
        "不做全局强制白平衡，以免消除舞台意图；降低总体饱和并强化高光/色域约束。",
    ),
    "day_for_night": _look(
        "Day for Night", "日拍夜", ["剧情特效"],
        ["降低曝光", "冷环境", "压低暖背景", "保留月光方向"], ["高光", "肤色", "天空"],
        {"exposure": -1.0, "contrast": 1.12, "saturation": 0.68, "temperature": -16.0,
         "highlight_softness": 0.55, "color_density": 0.08,
         "shadow_color": [-0.012, 0.001, 0.026],
         "gamut_protection": 1.0, "skin_protection": 0.95},
        "这是保守起点而非自动特效；月光方向、天空和人物通常需要镜头级局部遮罩完成。",
    ),
}


def list_look_packages() -> list[dict[str, Any]]:
    return [{"id": key, **deepcopy(value)} for key, value in LOOK_PACKAGES.items()]


def apply_look_package(recipe: GradeRecipe, look_id: str, strength: float = 1.0) -> GradeRecipe:
    if look_id not in LOOK_PACKAGES:
        raise ValueError(f"Unknown look package: {look_id}")
    amount = max(0.0, min(1.0, float(strength)))
    data = recipe.model_dump()
    defaults = deepcopy(LOOK_PACKAGES[look_id]["defaults"])
    # Keep the HDR shoulder explicit in the recipe. Existing look packages
    # predate the separate rolloff control, so inherit their highlight
    # softness until a package provides a dedicated value.
    defaults.setdefault("highlight_rolloff", defaults.get("highlight_softness", 0.0))
    neutral = {
        "exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "temperature": 0.0,
        "tint": 0.0, "highlight_rolloff": 0.0, "highlight_softness": 0.0, "color_density": 0.0,
        "shadow_color": [0.0, 0.0, 0.0], "gamut_protection": 0.0, "skin_protection": 0.0,
    }
    for key, target in defaults.items():
        current = data.get(key, neutral.get(key))
        base = neutral[key]
        if isinstance(target, list):
            data[key] = [float(current[i]) + (float(target[i]) - float(base[i])) * amount for i in range(3)]
        else:
            data[key] = float(current) + (float(target) - float(base)) * amount
    data["look_id"] = look_id
    data["look_strength"] = amount
    # Look values are blended with the current technical recipe.  A QC repair
    # may already have raised a protection control to its safety ceiling, so
    # adding the Look package must never create an invalid GradeRecipe (for
    # example skin_protection > 1.0).
    bounds = {
        "exposure": (-2.0, 2.0), "temperature": (-50.0, 50.0), "tint": (-0.5, 0.5),
        "contrast": (0.5, 1.5), "pivot": (0.01, 1.0), "saturation": (0.0, 2.0),
        "highlight_rolloff": (0.0, 1.0), "highlight_softness": (0.0, 1.0),
        "color_density": (-1.0, 1.0), "skin_protection": (0.0, 1.0),
        "gamut_protection": (0.0, 1.0), "look_strength": (0.0, 1.0),
    }
    for key, (lower, upper) in bounds.items():
        if key in data:
            data[key] = max(lower, min(upper, float(data[key])))
    sources = dict(data.get("parameter_sources") or {})
    confidence = dict(data.get("parameter_confidence") or {})
    for key in defaults:
        sources[key] = "look_package"
        confidence[key] = max(float(confidence.get(key, 0.0)), round(amount, 3))
    data["parameter_sources"] = sources
    data["parameter_confidence"] = confidence
    data["rationales"] = list(data.get("rationales") or []) + [
        f"Applied engineering Look Package '{look_id}' at {amount:.0%} strength."
    ]
    return GradeRecipe.model_validate(data)

#!/usr/bin/env python3
"""Fail-fast environment diagnostics for Local Color Service V0.1.0."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def _executable(env_name: str, requested: str, fallback_env: str, command: str) -> str:
    candidates = [os.getenv(env_name, requested), os.getenv(fallback_env, ""), shutil.which(command) or ""]
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), candidates[0])


def get_ffmpeg_path() -> str:
    return _executable("FFMPEG_PATH", r"C:\ffmpeg_cuda\bin\ffmpeg.exe", "FFMPEG_FALLBACK_PATH", "ffmpeg")


def get_ffprobe_path() -> str:
    return _executable("FFPROBE_PATH", r"C:\ffmpeg_cuda\bin\ffprobe.exe", "FFPROBE_FALLBACK_PATH", "ffprobe")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


def _load_studio_config(ocio: Any) -> tuple[Any, str]:
    configured = os.getenv("OCIO_CONFIG_PATH", "")
    if configured and Path(configured).is_file():
        return ocio.Config.CreateFromFile(configured), str(Path(configured).resolve())

    registry = ocio.BuiltinConfigRegistry()
    names = [entry[0] for entry in registry.getBuiltinConfigs() if entry[0].startswith("studio-config-")]
    errors: list[str] = []
    for name in reversed(names):
        try:
            return ocio.Config.CreateFromBuiltinConfig(name), name
        except Exception as exc:  # pragma: no cover - depends on installed OCIO bundle
            errors.append(f"{name}: {exc}")
    raise RuntimeError("No ACES Studio Config could be loaded: " + "; ".join(errors))


def _resolve_required_spaces(config: Any) -> dict[str, str]:
    names = [cs.getName() for cs in config.getColorSpaces()]
    working = next((n for n in names if n.casefold() == "acescct"), "")
    rec709 = [n for n in names if "rec.709" in n.casefold() or "rec709" in n.casefold()]
    encoded = [n for n in rec709 if "2.4" in n.casefold() and ("encoded" in n.casefold() or "texture" in n.casefold())]
    input_output = encoded[0] if encoded else next((n for n in rec709 if "camera" in n.casefold()), "")
    if not working or not input_output:
        raise RuntimeError(f"Required ACEScct/SDR Rec.709 spaces not found among {len(names)} enumerated spaces")
    config.getProcessor(input_output, working)
    config.getProcessor(working, input_output)
    return {"input": input_output, "working": working, "output": input_output}


def collect_diagnostics() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    add("Python 3.10", sys.version_info[:2] == (3, 10), f"Detected {py}")

    ffmpeg, ffprobe = get_ffmpeg_path(), get_ffprobe_path()
    add("FFmpeg executable", Path(ffmpeg).is_file(), ffmpeg)
    add("FFprobe executable", Path(ffprobe).is_file(), ffprobe)

    if Path(ffmpeg).is_file():
        version = _run([ffmpeg, "-version"])
        add("FFmpeg execution", version.returncode == 0, (version.stdout or version.stderr).splitlines()[0])
        filters = _run([ffmpeg, "-hide_banner", "-filters"])
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"])
        for feature in ("lut3d", "zscale"):
            add(f"FFmpeg filter {feature}", filters.returncode == 0 and feature in filters.stdout, "required")
        for feature in ("h264_nvenc", "hevc_nvenc"):
            add(f"FFmpeg encoder {feature}", encoders.returncode == 0 and feature in encoders.stdout, "required")

    ocio_details: dict[str, Any] = {}
    try:
        import PyOpenColorIO as ocio

        add("PyOpenColorIO import", True, ocio.__version__)
        config, config_id = _load_studio_config(ocio)
        spaces = _resolve_required_spaces(config)
        ocio_details = {"version": ocio.__version__, "config": config_id, "spaces": spaces}
        add("ACES Studio Config load", True, config_id)
        add("Enumerated ACEScct and Rec.709", True, json.dumps(spaces, ensure_ascii=False))
    except Exception as exc:
        add("PyOpenColorIO / ACES Studio", False, str(exc))

    try:
        import scenedetect
        add("PySceneDetect import", True, getattr(scenedetect, "__version__", "installed"))
    except Exception as exc:
        add("PySceneDetect import", False, str(exc))

    try:
        import torch
        add("PyTorch for AdaInt", True, f"{torch.__version__}; CUDA={torch.cuda.is_available()}")
    except Exception as exc:
        add("PyTorch for AdaInt", False, str(exc))
    adaint_checkpoint = Path(os.getenv("ADAINT_CHECKPOINT", PROJECT_ROOT / "models" / "adaint" / "AiLUT-FiveK-sRGB.pth"))
    add("AdaInt official checkpoint", adaint_checkpoint.is_file(), str(adaint_checkpoint.resolve()))

    gpu = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    add("NVIDIA GPU", gpu.returncode == 0 and bool(gpu.stdout.strip()), gpu.stdout.strip() or gpu.stderr.strip())

    data_dir = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
    writable = True
    details: list[str] = []
    for name in ("jobs",):
        directory = data_dir / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            marker = directory / ".doctor_write_test"
            marker.write_text("ok", encoding="utf-8")
            marker.unlink()
            details.append(f"{directory}: OK")
        except Exception as exc:
            writable = False
            details.append(f"{directory}: {exc}")
    add("Writable job directory", writable, "; ".join(details))

    return {
        "ok": all(item["ok"] for item in checks),
        "python": py,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "ocio": ocio_details,
        "checks": checks,
    }


def run_doctor() -> int:
    report = collect_diagnostics()
    print("Local Color Service V0.6.9 - Environment Doctor")
    print("-" * 100)
    for item in report["checks"]:
        print(f"{'PASS' if item['ok'] else 'FAIL':<5} {item['name']:<36} {item['detail']}")
    print("-" * 100)
    print("SUCCESS" if report["ok"] else "FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(run_doctor())

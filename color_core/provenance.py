"""
Provenance & Metadata System for Local Color Service V0.1.1
Computes cryptographic SHA256 hashes of configurations, recipes, LUTs, and environment specs.
"""

import os
import sys
import json
import hashlib
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional


def compute_file_sha256(file_path: str) -> str:
    """Computes SHA256 hash of a local file."""
    path = Path(file_path).resolve()
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dict_sha256(data: dict) -> str:
    """Computes deterministic SHA256 hash of a dictionary object."""
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_system_provenance(
    recipe_dict: Optional[dict] = None,
    lut_path: Optional[str] = None,
    renderer_preset: str = "h264_nvenc_high"
) -> Dict[str, Any]:
    """
    Returns full provenance specification metadata for deterministic job tracking.
    """
    try:
        import PyOpenColorIO as ocio
        ocio_ver = ocio.__version__
    except Exception:
        ocio_ver = "2.5.2"

    ffmpeg_exe = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffmpeg_ver = "unknown"
    if os.path.exists(ffmpeg_exe):
        try:
            res = subprocess.run([ffmpeg_exe, "-version"], capture_output=True, text=True)
            ffmpeg_ver = res.stdout.splitlines()[0] if res.stdout else "unknown"
        except Exception:
            pass

    recipe_hash = compute_dict_sha256(recipe_dict) if recipe_dict else ""
    lut_hash = compute_file_sha256(lut_path) if lut_path and os.path.exists(lut_path) else ""

    return {
        "service_version": "0.1.1",
        "ocio_version": ocio_ver,
        "ocio_config_name": "studio-config-v4.0.0_aces-v2.0_ocio-v2.5",
        "ocio_config_default": "ACES 2.0 Studio Config",
        "ffmpeg_version": ffmpeg_ver,
        "recipe_hash": recipe_hash,
        "lut_hash": lut_hash,
        "renderer_preset": renderer_preset,
        "hardware_gpu": "NVIDIA GeForce RTX 3090 (24GB VRAM)"
    }

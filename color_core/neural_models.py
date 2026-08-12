"""Real model adapters for V0.2.

AdaInt uses a portable PyTorch implementation of the official inference graph.  It
loads the official checkpoint but does not depend on the legacy MMCV/MMEditing
stack or its Python-3.7-only CUDA extension because the service needs the
generated LUT, not the per-pixel training operator.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from scipy.interpolate import RegularGridInterpolator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_adaint_checkpoint() -> Path:
    return PROJECT_ROOT / "models" / "adaint" / "AiLUT-FiveK-sRGB.pth"


def neural_runtime_status() -> dict[str, Any]:
    checkpoint = Path(os.getenv("ADAINT_CHECKPOINT", str(_default_adaint_checkpoint()))).resolve()
    try:
        import torch
        torch_version = torch.__version__
        cuda_available = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if cuda_available else "CPU"
    except ImportError:
        torch_version, cuda_available, device = "not installed", False, "unavailable"
    reference_command = os.getenv("REFERENCE_LUT_COMMAND", "").strip()
    embedding_backend = os.getenv("SCENE_EMBEDDING_BACKEND", "offline").casefold()
    dinov2_repo = os.getenv("DINOV2_REPO", "").strip()
    from color_core.canoncgt_provider import canoncgt_runtime_status
    return {
        "adaint": {
            "runtime": "portable-official-checkpoint",
            "available": torch_version != "not installed" and checkpoint.is_file(),
            "checkpoint": str(checkpoint), "torch_version": torch_version,
            "cuda_available": cuda_available, "device": device,
        },
        "reference_lut_diffusion": {
            "runtime": "external-command-adapter", "available": bool(reference_command),
            "command_configured": bool(reference_command),
        },
        "canoncgt": canoncgt_runtime_status(),
        "face_analysis": {
            "runtime": "opencv-haar-ycrcb", "available": True,
            "selective_render": os.getenv("FACE_SELECTIVE_RENDER", "1") != "0",
            "creative_strength": float(os.getenv("FACE_CREATIVE_STRENGTH", "0.30")),
        },
        "scene_embedding": {
            "runtime": embedding_backend,
            "available": embedding_backend != "dinov2" or bool(dinov2_repo) or os.getenv("DINOV2_ALLOW_DOWNLOAD", "0") == "1",
            "dinov2_repo": dinov2_repo,
            "fallback": "offline_hybrid",
        },
    }


def _torch_classes():
    try:
        import torch
        import torch.nn as nn
        import torch.nn.functional as functional
    except ImportError as exc:
        raise RuntimeError("AdaInt requires PyTorch; install the V0.2 neural dependencies") from exc

    class BasicBlock(nn.Sequential):
        def __init__(self, in_channels, out_channels, stride=1, norm=False):
            body = [nn.Conv2d(in_channels, out_channels, 3, stride=stride, padding=1), nn.LeakyReLU(0.2)]
            if norm:
                body.append(nn.InstanceNorm2d(out_channels, affine=True))
            super().__init__(*body)

    class TPAMIBackbone(nn.Sequential):
        def __init__(self):
            super().__init__(
                BasicBlock(3, 16, stride=2, norm=True), BasicBlock(16, 32, stride=2, norm=True),
                BasicBlock(32, 64, stride=2, norm=True), BasicBlock(64, 128, stride=2, norm=True),
                BasicBlock(128, 128, stride=2), nn.Dropout(p=0.5), nn.AdaptiveAvgPool2d(2),
            )

        def forward(self, images):
            images = functional.interpolate(images, size=(256, 256), mode="bilinear", align_corners=False)
            return super().forward(images).view(images.shape[0], -1)

    class PortableAiLUT(nn.Module):
        def __init__(self, n_vertices=33, n_ranks=3):
            super().__init__()
            self.n_vertices = n_vertices
            self.backbone = TPAMIBackbone()
            self.lut_generator = nn.Module()
            self.lut_generator.weights_generator = nn.Linear(512, n_ranks)
            self.lut_generator.basis_luts_bank = nn.Linear(n_ranks, 3 * n_vertices ** 3, bias=False)
            self.adaint = nn.Module()
            self.adaint.intervals_generator = nn.Linear(512, 3 * (n_vertices - 1))

        def forward(self, images):
            codes = self.backbone(images)
            weights = self.lut_generator.weights_generator(codes)
            luts = self.lut_generator.basis_luts_bank(weights).view(-1, 3, self.n_vertices, self.n_vertices, self.n_vertices)
            intervals = self.adaint.intervals_generator(codes).view(-1, 3, self.n_vertices - 1).softmax(-1)
            vertices = functional.pad(intervals.cumsum(-1), (1, 0), "constant", 0)
            return luts, vertices, weights

    return torch, PortableAiLUT


def _state_dict_from_checkpoint(checkpoint: Path, torch) -> dict:
    loaded = torch.load(str(checkpoint), map_location="cpu", weights_only=False)
    state = loaded.get("state_dict", loaded) if isinstance(loaded, dict) else loaded
    if not isinstance(state, dict):
        raise RuntimeError("Unsupported AdaInt checkpoint structure")
    cleaned = {}
    for key, value in state.items():
        name = key
        for prefix in ("module.", "model.", "generator."):
            if name.startswith(prefix):
                name = name[len(prefix):]
        cleaned[name] = value
    return cleaned


def _write_cube(values: np.ndarray, output_path: str, title: str) -> str:
    size = values.shape[0]
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    lines.extend("{:.8f} {:.8f} {:.8f}".format(*np.clip(rgb, 0.0, 1.0)) for rgb in values.reshape(-1, 3))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def generate_adaint_lut(frame_bgr: np.ndarray, output_path: str, checkpoint_path: str | None = None, size: int = 33) -> dict[str, Any]:
    """Run the official AdaInt network graph and resample its adaptive lattice."""
    checkpoint = Path(checkpoint_path or os.getenv("ADAINT_CHECKPOINT", str(_default_adaint_checkpoint()))).resolve()
    if not checkpoint.is_file():
        raise RuntimeError(f"AdaInt checkpoint is not installed: {checkpoint}")
    torch, model_class = _torch_classes()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model_class().to(device)
    state = _state_dict_from_checkpoint(checkpoint, torch)
    compatible = {key: value for key, value in state.items() if key in model.state_dict()}
    missing, unexpected = model.load_state_dict(compatible, strict=False)
    essential_missing = [key for key in missing if not key.endswith("num_batches_tracked")]
    if essential_missing:
        raise RuntimeError(f"AdaInt checkpoint is incompatible; missing keys: {essential_missing[:8]}")
    model.eval()
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = torch.from_numpy(rgb.transpose(2, 0, 1)).unsqueeze(0).to(device)
    with torch.inference_mode():
        adaptive_lut, vertices, weights = model(tensor)
    lut = adaptive_lut[0].detach().cpu().numpy()
    verts = vertices[0].detach().cpu().numpy()
    grid = np.linspace(0.0, 1.0, size)
    blue, green, red = np.meshgrid(grid, grid, grid, indexing="ij")
    points = np.stack([blue, green, red], axis=-1)
    output = np.empty((size, size, size, 3), dtype=np.float64)
    source_axes = (verts[2], verts[1], verts[0])
    for channel in range(3):
        interpolator = RegularGridInterpolator(source_axes, lut[channel], bounds_error=False, fill_value=None)
        output[..., channel] = interpolator(points)
    path = _write_cube(output, output_path, "LocalColorService AdaInt FiveK sRGB")
    return {
        "lut_path": path, "model": "AdaInt AiLUT-FiveK-sRGB", "checkpoint": str(checkpoint),
        "device": str(device), "weights": weights[0].detach().cpu().tolist(),
        "unexpected_checkpoint_keys": unexpected[:8],
    }


def run_reference_lut_model(source_image: str, reference_image: str, output_path: str, variant: str = "A") -> dict[str, Any]:
    """Invoke a separately managed diffusion model runner without polluting this environment."""
    template = os.getenv("REFERENCE_LUT_COMMAND", "").strip()
    if not template:
        raise RuntimeError("REFERENCE_LUT_COMMAND is not configured")
    command = template.format(source=source_image, reference=reference_image, output=output_path, variant=variant)
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=1800, check=False)
    output = Path(output_path).resolve()
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError(f"Reference LUT model failed: {result.stderr[-4000:]}")
    return {"lut_path": str(output), "runtime": "external", "stdout": result.stdout[-2000:]}

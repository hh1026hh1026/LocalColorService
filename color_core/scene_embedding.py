"""Scene embeddings with an offline descriptor and optional local DINOv2."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


def _offline_descriptor(frame: np.ndarray) -> np.ndarray:
    small = cv2.resize(frame, (96, 96), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256]).reshape(-1)
    hist /= max(float(np.linalg.norm(hist)), 1e-8)
    grid = cv2.resize(cv2.cvtColor(small, cv2.COLOR_BGR2Lab), (4, 4), interpolation=cv2.INTER_AREA)
    grid = grid.astype(np.float32).reshape(-1) / 255.0
    edges = cv2.Canny(small, 80, 160)
    edge_grid = cv2.resize(edges, (4, 4), interpolation=cv2.INTER_AREA).reshape(-1).astype(np.float32) / 255.0
    vector = np.concatenate((hist, grid, edge_grid)).astype(np.float32)
    return vector / max(float(np.linalg.norm(vector)), 1e-8)


@lru_cache(maxsize=1)
def _dinov2_model():
    import torch

    repo = os.getenv("DINOV2_REPO", "").strip()
    if repo and Path(repo).is_dir():
        model = torch.hub.load(repo, "dinov2_vits14", source="local")
    elif os.getenv("DINOV2_ALLOW_DOWNLOAD", "0") == "1":
        model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
    else:
        raise RuntimeError("DINOv2 is not locally configured")
    return model.eval()


def scene_embedding(frames: list[np.ndarray]) -> tuple[list[float], str]:
    backend = os.getenv("SCENE_EMBEDDING_BACKEND", "offline").casefold()
    if backend == "dinov2":
        try:
            import torch
            import torch.nn.functional as functional

            model = _dinov2_model()
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            model = model.to(device)
            tensors = []
            for frame in frames:
                rgb = cv2.cvtColor(cv2.resize(frame, (224, 224)), cv2.COLOR_BGR2RGB)
                tensor = torch.from_numpy(rgb).permute(2, 0, 1).float() / 255.0
                tensor = (tensor - torch.tensor([0.485, 0.456, 0.406])[:, None, None]) / torch.tensor([0.229, 0.224, 0.225])[:, None, None]
                tensors.append(tensor)
            with torch.inference_mode():
                embedding = model(torch.stack(tensors).to(device)).mean(dim=0)
                embedding = functional.normalize(embedding, dim=0).cpu().numpy()
            return [round(float(value), 7) for value in embedding], "dinov2_vits14"
        except Exception as exc:
            # DINOv2 was explicitly requested, so falling back to the offline
            # descriptor is a downgrade the operator should hear about rather
            # than discover from a changed backend name in a report.
            import logging

            from color_core.degradation import report_degradation

            report_degradation(logging.getLogger("local_color"), "DINOv2 scene embedding", exc)
    vectors = [_offline_descriptor(frame) for frame in frames]
    embedding = np.mean(vectors, axis=0)
    embedding /= max(float(np.linalg.norm(embedding)), 1e-8)
    return [round(float(value), 7) for value in embedding], "offline_hybrid"

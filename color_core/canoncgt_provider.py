"""CanonCGT reference provider with explicit LUT fitting and safe fallback.

CanonCGT itself produces graded images. This adapter runs it out-of-process,
fits aligned source/target frames to uniform CUBE assets, validates those assets,
and falls back to the deterministic statistical route when the model runtime is
not installed or inference fails.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from color_core.look_provider import CandidateScore, LookCandidate, LookProvider, ProviderCapabilities
from color_core.lut_fitter import fit_lut_from_pairs
from color_core.ocio_manager import OCIOManager
from color_core.project_qc import inspect_cube_lut
from color_core.reference_candidates import generate_reference_candidates
from color_core.cube_tools import apply_cube_to_bgr
from color_core.face_analysis import skin_mask_bgr


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _validate_candidate(source_frames: list[np.ndarray], lut_path: str) -> dict[str, float]:
    outputs = [apply_cube_to_bgr(frame, lut_path) for frame in source_frames]

    def signature(frame: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
        return np.asarray([np.median(lum), *np.mean(rgb, axis=(0, 1))], dtype=np.float64)

    source_signatures = [signature(frame) for frame in source_frames]
    output_signatures = [signature(frame) for frame in outputs]
    added_jumps = []
    for index in range(1, len(source_frames)):
        source_jump = float(np.linalg.norm(source_signatures[index] - source_signatures[index - 1]))
        output_jump = float(np.linalg.norm(output_signatures[index] - output_signatures[index - 1]))
        added_jumps.append(max(0.0, output_jump - source_jump))
    continuity = float(np.clip(1.0 - (np.mean(added_jumps) if added_jumps else 0.0) * 5.0, 0.0, 1.0))

    skin_deltas = []
    for source, output in zip(source_frames, outputs):
        mask, boxes = skin_mask_bgr(source)
        selected = mask >= 64
        if boxes and np.count_nonzero(selected) >= 128:
            source_lab = cv2.cvtColor(source, cv2.COLOR_BGR2LAB).astype(np.float32)
            output_lab = cv2.cvtColor(output, cv2.COLOR_BGR2LAB).astype(np.float32)
            skin_deltas.append(float(np.linalg.norm(np.mean(output_lab[selected], axis=0) - np.mean(source_lab[selected], axis=0))))
    skin_safety = float(np.clip(1.0 - (np.mean(skin_deltas) if skin_deltas else 0.0) / 45.0, 0.0, 1.0))
    clipping = np.mean([
        np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).max(axis=2) >= 253) for frame in outputs
    ])
    clipping_safety = float(np.clip(1.0 - clipping / 0.10, 0.0, 1.0))
    return {
        "continuity": round(continuity, 6), "skin_safety": round(skin_safety, 6),
        "clipping_safety": round(clipping_safety, 6),
    }


def _runtime_paths() -> tuple[Path, Path, str]:
    root = Path(os.getenv("CANONCGT_ROOT", str(PROJECT_ROOT / "models" / "canoncgt" / "CanonCGT"))).resolve()
    checkpoint = Path(
        os.getenv("CANONCGT_CHECKPOINT", str(root / "pretrained" / "SSL_updated_251111.pth"))
    ).resolve()
    python_exe = os.getenv(
        "CANONCGT_PYTHON", r"C:\Users\Administrator\.conda\envs\localcolor\python.exe"
    )
    return root, checkpoint, python_exe


def canoncgt_runtime_status() -> dict[str, Any]:
    root, checkpoint, python_exe = _runtime_paths()
    command_template = os.getenv("CANONCGT_COMMAND", "").strip()
    bundled = (root / "demo.py").is_file() and checkpoint.is_file()
    return {
        "available": bool(command_template) or bundled,
        "runtime": "external-command" if command_template else "bundled-subprocess",
        "root": str(root),
        "checkpoint": str(checkpoint),
        "python": python_exe,
        "command_configured": bool(command_template),
        "detail": "ready" if bool(command_template) or bundled else "install CanonCGT source and SSL.pth",
    }


def inspect_reference_frame(reference_bgr: np.ndarray) -> dict[str, Any]:
    if reference_bgr is None or reference_bgr.size == 0:
        raise ValueError("reference frame is empty")
    rgb = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    luminance = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    low = float(np.mean(luminance < 0.01))
    high = float(np.mean(luminance > 0.99))
    warnings: list[str] = []
    if min(reference_bgr.shape[:2]) < 256:
        warnings.append("reference resolution is low")
    if high > 0.05:
        warnings.append("reference contains substantial highlight clipping")
    if low > 0.10:
        warnings.append("reference contains substantial shadow clipping")
    return {
        "width": int(reference_bgr.shape[1]), "height": int(reference_bgr.shape[0]),
        "black_clipping_ratio": round(low, 6), "highlight_clipping_ratio": round(high, 6),
        "warnings": warnings, "passed": high <= 0.20 and low <= 0.35,
    }


class CanonCGTProvider(LookProvider):
    def __init__(self, manager: OCIOManager):
        self.manager = manager

    def capabilities(self) -> ProviderCapabilities:
        status = canoncgt_runtime_status()
        return ProviderCapabilities(
            provider="canoncgt", supports_reference=True, direct_lut_output=False,
            requires_lut_fitting=True, recommended_scope="scene_group", experimental=True,
            available=bool(status["available"]), detail=str(status["detail"]),
        )

    @staticmethod
    def _run_target(source_path: Path, reference_path: Path, output_path: Path) -> None:
        template = os.getenv("CANONCGT_COMMAND", "").strip()
        if template:
            command = template.format(source=str(source_path), reference=str(reference_path), output=str(output_path))
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=1800, check=False)
        else:
            root, checkpoint, python_exe = _runtime_paths()
            command = [
                python_exe, "demo.py", "--gpu", "0", "--pretrained_path", str(checkpoint),
                "--inp_path", str(source_path), "--ref_path", str(reference_path),
                "--out_path", str(output_path),
            ]
            result = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=1800, check=False)
        if result.returncode != 0 or not output_path.is_file():
            raise RuntimeError("CanonCGT inference failed: " + (result.stderr or result.stdout)[-3000:])

    def _fallback(self, source: np.ndarray, reference: np.ndarray, output_dir: Path, reason: str) -> list[LookCandidate]:
        items = generate_reference_candidates(source, reference, str(output_dir), self.manager)
        return [
            LookCandidate(
                id=item["id"], label=f"{item['label']}（统计回退）", provider="statistical",
                lut_path=item["lut_path"], strength=item["strength"], fallback_used=True,
                warnings=[reason], metadata={"look_id": item["look_id"], "fallback_reason": reason},
                score=CandidateScore(technical_safety=1.0, continuity=0.8, reference_match=0.45, total=0.69),
            )
            for item in items
        ]

    def generate_candidates(self, context: dict[str, Any], count: int = 3) -> list[LookCandidate]:
        source_frames: list[np.ndarray] = context["source_frames"]
        reference: np.ndarray = context["reference_frame"]
        output_dir = Path(context["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        allow_fallback = bool(context.get("allow_fallback", True))
        status = canoncgt_runtime_status()
        if not status["available"]:
            if not allow_fallback:
                raise RuntimeError(status["detail"])
            return self._fallback(source_frames[0], reference, output_dir, str(status["detail"]))

        reference_path = output_dir / "canoncgt_reference.png"
        if not cv2.imwrite(str(reference_path), reference):
            raise RuntimeError("could not persist CanonCGT reference frame")
        targets: list[np.ndarray] = []
        try:
            for index, source in enumerate(source_frames):
                source_path = output_dir / f"canoncgt_source_{index:02d}.png"
                target_path = output_dir / f"canoncgt_target_{index:02d}.png"
                if not cv2.imwrite(str(source_path), source):
                    raise RuntimeError("could not persist CanonCGT source frame")
                self._run_target(source_path, reference_path, target_path)
                target = cv2.imread(str(target_path))
                if target is None:
                    raise RuntimeError("CanonCGT target image is unreadable")
                targets.append(target)
        except Exception as exc:
            if not allow_fallback:
                raise
            return self._fallback(source_frames[0], reference, output_dir, str(exc))

        safe = generate_reference_candidates(source_frames[0], reference, str(output_dir / "safe"), self.manager)[0]
        candidates = [LookCandidate(
            id="A", label="统计安全", provider="statistical", lut_path=safe["lut_path"],
            strength=safe["strength"],
            score=CandidateScore(technical_safety=1.0, continuity=0.9, reference_match=0.5, total=0.735),
            metadata={"look_id": safe["look_id"]},
        )]
        for candidate_id, label, strength in (("B", "CanonCGT 标准", 0.65), ("C", "CanonCGT 接近参考", 0.85)):
            # A neural reference match can be visually convincing but too
            # aggressive for one global LUT.  Retry with bounded creative
            # strength until monotonicity and skin-safety checks pass.
            requested_strength = float(strength)
            attempts = []
            for attempt in (requested_strength, requested_strength * 0.70, requested_strength * 0.50, 0.25, 0.15, 0.08):
                value = round(max(0.05, min(1.0, attempt)), 3)
                if value not in attempts:
                    attempts.append(value)
            fitted = None
            report = None
            for attempt in attempts:
                candidate = fit_lut_from_pairs(
                    source_frames, targets, str(output_dir / f"candidate_{candidate_id.lower()}.cube"),
                    size=int(context.get("lut_size", 33)), strength=attempt,
                )
                inspected = inspect_cube_lut(candidate["lut_path"], "creative")
                fitted, report = candidate, inspected
                if inspected.passed:
                    break
            assert fitted is not None and report is not None
            validation = _validate_candidate(source_frames, fitted["lut_path"])
            match = float(np.clip(1.0 - fitted["fit_rmse"] * 4.0, 0.0, 1.0))
            technical = float(report.passed) * min(validation["skin_safety"], validation["clipping_safety"])
            continuity = validation["continuity"]
            total = 0.30 * technical + 0.25 * continuity + 0.45 * match
            candidates.append(LookCandidate(
                id=candidate_id, label=label, provider="canoncgt", lut_path=fitted["lut_path"],
                strength=strength, passed=report.passed, warnings=list(report.warnings),
                score=CandidateScore(
                    technical_safety=technical,
                    continuity=continuity, reference_match=match, total=float(np.clip(total, 0.0, 1.0)),
                ),
                metadata={
                    **fitted, **validation, "runtime": status["runtime"],
                    "requested_strength": requested_strength,
                    "strength_retries": attempts[:attempts.index(float(fitted["strength"])) + 1],
                },
            ))
        return candidates[:count]

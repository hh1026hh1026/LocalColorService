# Local Color Service - Dependency Audit Report (DEPENDENCY_AUDIT.md)

**Audit Date**: 2026-08-03  
**Target Platform**: Windows 11 64-bit  
**Target Hardware**: NVIDIA GeForce RTX 3090 (24GB VRAM)  

---

## 1. System Environment Audit

| Resource | Target Requirement | Detected System Status | Audit Result | Action / Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| **OS** | Windows 11 | Windows 11 (Build 26100) | **PASS** | Native Windows support |
| **Python** | Python 3.10 | Conda Env `localcolor` (Python 3.10.20) | **PASS** | Isolated conda environment |
| **GPU** | NVIDIA RTX 3090 24GB | NVIDIA GeForce RTX 3090 (24GB VRAM) | **PASS** | Ready for NVENC acceleration |
| **CUDA Driver** | CUDA 11.x/12.x/13.x | Driver 596.21 / CUDA 13.2 | **PASS** | Fully supported |
| **FFmpeg Binary** | `C:\ffmpeg_cuda\bin\ffmpeg.exe` | Found at `E:\ffmpeg-2025-01-15...` | **PASS / CONFIGURED** | Support `h264_nvenc`, `hevc_nvenc`, `lut3d` (tetrahedral), `zscale`. Auto-detect or set in `.env` |
| **FFprobe Binary** | `C:\ffmpeg_cuda\bin\ffprobe.exe` | Found at `E:\ffmpeg-2025-01-15...` | **PASS / CONFIGURED** | Used for JSON media probing |

---

## 2. Python Package Dependency Matrix

The following libraries are required for the V0.1 release of Local Color Service:

| Package | Minimum Version | Recommended Version | Purpose |
| :--- | :--- | :--- | :--- |
| `fastapi` | `>=0.100.0` | `0.110.0` | High-performance REST API engine |
| `uvicorn[standard]` | `>=0.20.0` | `0.28.0` | ASGI HTTP Server for FastAPI |
| `pydantic` | `>=2.0.0` | `2.6.4` | Data validation & schema serialization (`grade_recipe.json`) |
| `opencolorio` | `>=2.1.0` | `2.3.0` | Color management engine, OCIO 3D LUT baker |
| `colour-science` | `>=0.4.2` | `0.4.4` | Colorimetry analysis, ACEScct conversions, delta E |
| `opencv-python` | `>=4.8.0` | `4.9.0.80` | Frame decoding, histogram analysis, white balance estimation |
| `numpy` | `>=1.24.0, <2.0.0` | `1.26.4` | Numerical matrix calculations for color analysis |
| `python-dotenv` | `>=1.0.0` | `1.0.1` | Config management via `.env` file |
| `aiofiles` | `>=23.0.0` | `23.2.1` | Async file operations for REST API |
| `httpx` | `>=0.25.0` | `0.27.0` | Async test client for FastAPI integration tests |
| `pytest` | `>=7.4.0` | `8.1.1` | Comprehensive unit and integration test framework |
| `pytest-asyncio` | `>=0.21.0` | `0.23.5` | Async test runner support for FastAPI endpoints |

---

## 3. FFmpeg Capabilities Audit

The detected FFmpeg build was audited for key filters and hardware encoders:

- **`h264_nvenc`**: **AVAILABLE** (Hardware accelerated H.264 encoding on RTX 3090)
- **`hevc_nvenc`**: **AVAILABLE** (Hardware accelerated HEVC H.265 encoding on RTX 3090)
- **`lut3d` filter**: **AVAILABLE** (Supports tetrahedral interpolation `-vf "lut3d=file=...:interp=tetrahedral"`)
- **`zscale` filter**: **AVAILABLE** (Used for color space / matrix conversion when needed)
- **Audio pass-through**: Supported via `-c:a copy` or re-encoding.

---

## 4. OpenColorIO (OCIO) & ACES 2.0 Config Audit

- **OCIO Engine**: PyOpenColorIO `opencolorio` 2.x python bindings.
- **Color Spaces**:
  - **Input**: Rec.709 (sRGB / Rec.709 Gamma 2.4)
  - **Working**: ACEScct (`ACES - ACEScct`)
  - **Output**: Rec.709 (`Output - Rec.709` / `sRGB - Texture`)
- **OCIO Config Source**: Enumerated built-in `studio-config-v4.0.0_aces-v2.0_ocio-v2.5`; the configured local path is used only when it exists and validates successfully.

---

## 5. Dependency Audit Conclusion

All required system dependencies, hardware capabilities, and software libraries are validated and compatible with Windows 11 + NVIDIA RTX 3090 + Python 3.10.

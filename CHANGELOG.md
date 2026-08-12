# Changelog

All notable changes to **LocalColorService** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v0.1.0] - 2026-08-12

### Initial Open Source Release

#### Added
- **Core Processing Engine**:
  - PySceneDetect automated shot splitting & representative frame sampling.
  - Neutral Broadcast, Clean Commercial, and Cinematic Soft Print color recipes.
  - Multi-lane asynchronous background job worker architecture (`heavy` rendering & `light` interactive queries).
- **Advanced Color Grading & Neural Integration**:
  - Support for **AdaInt** LUT inference and uniform `.cube` generation.
  - Support for **CanonCGT** reference-image-based color transfer.
  - Reinhard & CLAHE adaptive color transformation fallback providers.
- **OCIO & Pipeline Controls**:
  - OpenColorIO (OCIO) GroupTransform manager with 33³ and 65³ `.cube` export.
  - Selective face protection mask generation (`FACE_CREATIVE_STRENGTH`) to preserve skin tones during aggressive creative grades.
- **Quality Control (QC) & Provenance**:
  - 4-level automated project QC (Black/white clipping, color shift, face mask sanity, LUT sanity).
  - SHA-256 asset freezing and provenance tracking (`artifact_freeze.py`).
- **REST API & Web Workbench**:
  - Full OpenAPI/Swagger `/docs` documentation.
  - Interactive browser-based Color Grading Workbench UI (`static/index.html`).
  - Operational Task Manager dashboard (`static/tasks.html`).
- **Open Source Infrastructure**:
  - MIT License (`LICENSE`).
  - Comprehensive `.gitignore` for video processing environments.
  - Community Issue templates (`.github/ISSUE_TEMPLATE`) and PR template (`.github/PULL_REQUEST_TEMPLATE.md`).
  - Contribution guide (`CONTRIBUTING.md`) and Code of Conduct (`CODE_OF_CONDUCT.md`).

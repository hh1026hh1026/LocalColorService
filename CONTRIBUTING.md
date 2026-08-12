# Contributing to LocalColorService

Thank you for your interest in contributing to **LocalColorService**! We welcome bug reports, feature proposals, documentation improvements, and pull requests from the community.

---

## 1. Code of Conduct

Please review and adhere to our [Code of Conduct](CODE_OF_CONDUCT.md) in all interactions within this project's repositories, issue trackers, and discussion boards.

---

## 2. Getting Started & Development Setup

### Prerequisites
- **Python**: `>= 3.10`
- **FFmpeg**: Built with NVDEC/NVENC support (recommended for GPU acceleration).
- **PyTorch**: Required for AdaInt and CanonCGT models (CUDA 12.8 support recommended).

### Environment Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/hh1026hh1026/LocalColorService.git
   cd LocalColorService
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   # On Windows:
   .venv\Scripts\activate
   # On Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Install CanonCGT (Optional / Neural Features)**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_canoncgt.ps1
   ```

5. **Start the API server**:
   ```bash
   python -m uvicorn app.main:app --reload
   ```

---

## 3. Running Tests & Diagnostics

Before submitting a Pull Request, verify that all existing tests pass and system diagnostics are clean:

```bash
# Run test suite
python -m pytest -q

# Run system diagnostic doctor
python scripts/doctor.py
```

---

## 4. Submitting Pull Requests (PRs)

When opening a Pull Request, please follow these guidelines:

1. **Keep Changes Focused**: Address a single feature, bugfix, or refactoring goal per PR.
2. **Preserve API Compatibility**: Do not break existing endpoint schemas (`/v1/color/*`) without prior discussion in an Issue.
3. **Add Unit Tests**: Add test cases under `tests/` for any new engine or API features.
4. **Update Documentation**: Update `README.md` or docstrings if introducing new environment variables or API parameters.
5. **Use Descriptive Commit Messages**: Standard format e.g. `feat: add custom 3D LUT caching`, `fix: handle edge case in white balance calculation`.

---

## 5. Reporting Issues & Feature Proposals

- **Bug Reports**: Use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.yml) and include full system info (OS, Python version, FFmpeg build, GPU model).
- **Feature Requests**: Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.yml) to detail the problem statement and proposed solution.

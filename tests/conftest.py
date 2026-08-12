import os
import shutil
import tempfile
from pathlib import Path

# This runs before test modules import ``app``.  The application has module
# level database and job-directory singletons, therefore redirecting DATA_DIR
# here prevents a test client from recovering or executing a user's jobs.
_TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-runtime"
_TEST_ROOT.mkdir(exist_ok=True)
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="pytest-", dir=_TEST_ROOT))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["LOCAL_COLOR_TESTING"] = "1"

import numpy as np
import pytest


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture
def neutral_bgr():
    return np.full((64, 64, 3), 128, dtype=np.uint8)

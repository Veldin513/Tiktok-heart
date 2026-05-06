from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


_TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="yara_pytest_"))
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("YARA_BASE_DIR", str(_TEST_RUNTIME))
os.environ.setdefault("TG_DISABLE_NOTIFICATIONS", "1")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

source_pool = _PROJECT_ROOT / "message_pool.txt"
if source_pool.exists():
    shutil.copy2(source_pool, _TEST_RUNTIME / "message_pool.txt")


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ARG001
    shutil.rmtree(_TEST_RUNTIME, ignore_errors=True)

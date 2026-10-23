from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('local_exists,override', [(True, None), (False, None), (True, 'custom-browsers')])
def test_worker_browser_storage_selection(tmp_path, local_exists, override):
    local_dir = tmp_path / '.playwright-browsers'
    if local_exists:
        local_dir.mkdir()
    env = os.environ.copy()
    env['YARA_BASE_DIR'] = str(tmp_path)
    env.pop('PLAYWRIGHT_BROWSERS_PATH', None)
    if override is not None:
        env['PLAYWRIGHT_BROWSERS_PATH'] = override
    result = subprocess.run(
        [sys.executable, '-c',
         "import os; import yara_app.config; print(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '<unset>'))"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, check=True,
    )
    expected = override if override is not None else str(local_dir) if local_exists else '<unset>'
    assert result.stdout.strip() == expected

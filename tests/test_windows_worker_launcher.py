from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows Script Host launcher')
def test_scheduled_launcher_translates_startup_and_self_test_switches(tmp_path: Path) -> None:
    script_dir = tmp_path / 'scripts' / 'windows'
    script_dir.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / 'scripts' / 'windows' / 'start_worker.vbs'
    launcher = script_dir / 'start_worker.vbs'
    shutil.copy2(source, launcher)
    (script_dir / 'start_worker.ps1').write_text(
        'param([switch]$BootForce, [switch]$SelfTest, [switch]$NoBrowser)\n'
        '@{ boot = [bool]$BootForce; self_test = [bool]$SelfTest; no_browser = [bool]$NoBrowser } | '
        'ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot "received.json") -Encoding UTF8\n',
        encoding='utf-8',
    )

    result = subprocess.run(
        ['cscript.exe', '//B', '//Nologo', str(launcher), '--boot-force', '--self-test', '--no-browser'],
        capture_output=True,
        timeout=20,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert result.returncode == 0
    assert json.loads((script_dir / 'received.json').read_text(encoding='utf-8-sig')) == {
        'boot': True, 'self_test': True, 'no_browser': True,
    }

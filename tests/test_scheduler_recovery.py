from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from scripts import start_worker_once as starter
from yara_app.project_adapter import ProjectAdapter, WorkerStatus
from yara_app.ttbot.models import safe_name_key


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    control = tmp_path / 'control'
    control.mkdir()
    (control / 'profiles.json').write_text(json.dumps({'alpha': [{'name': 'target', 'url': '@target'}]}))
    state = {'active_profile': 'alpha', 'cooldown_hours': 12}
    (control / 'control_state.json').write_text(json.dumps(state))
    adapter = ProjectAdapter(tmp_path)
    starts = []
    updates = []
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=False, pid=None))
    monkeypatch.setattr(adapter, 'start_worker', lambda: starts.append(True) or WorkerStatus(running=True, pid=123))
    monkeypatch.setattr(adapter.scheduler, 'status_cached', lambda **_kwargs: {
        'repeat_task_installed': True, 'enabled': True, 'every_12_hours': True,
        'next_run_time': '2026-10-24 12:54:16',
    })
    monkeypatch.setattr(adapter, '_run_powershell_command', updates.append)
    monkeypatch.setattr(starter, 'ProjectAdapter', lambda *_a: adapter)
    monkeypatch.setattr(sys, 'argv', ['start_worker_once.py'])
    monkeypatch.setenv('WORKER_COOLDOWN_GRACE_SECONDS', '0')
    return adapter, starts, updates


def test_manual_send_aligns_next_run_with_cooldown(runtime, monkeypatch):
    adapter, starts, updates = runtime
    last_send = time.mktime(time.strptime('2026-10-23 19:48:14', '%Y-%m-%d %H:%M:%S'))
    state_dir = adapter._get_profile_state_dir()
    state_dir.mkdir(parents=True)
    (state_dir / f'last_send_{safe_name_key("target")}.txt').write_text(str(last_send))
    # The old fixed 00:54 tick skipped, and re-registering the task was denied.
    now = [last_send + 5 * 3600 + 6 * 60]
    monkeypatch.setattr(starter.time, 'time', lambda: now[0])
    monkeypatch.setattr(starter, 'configure_boot_force_environment', lambda *_a, **_k: pytest.fail('normal tick must not bypass cooldown'))
    assert starter.main() == 0
    assert starts == []
    assert len(updates) == 1
    assert '2026-10-24 07:48:14' in updates[0]
    assert 'Set-ScheduledTask' in updates[0]
    assert '-Action' not in updates[0] and '-Principal' not in updates[0]
    assert 'PT5M' not in updates[0]
    # The adjusted one-shot boundary starts the worker when the target is due.
    now[0] = last_send + 12 * 3600 + 46
    assert starter.main() == 0
    assert starts == [True]
    # A duplicate task launch after sending must not send again.
    (state_dir / f'last_send_{safe_name_key("target")}.txt').write_text(str(now[0]))
    now[0] += 300
    assert starter.main() == 0
    assert starts == [True]


@pytest.mark.parametrize('blocker', ['paused', 'stop_requested', 'session_import_required', 'auth_backoff', 'session_import_running'])
def test_recovery_tick_honours_local_blockers_without_starting_browser(runtime, blocker):
    adapter, starts, updates = runtime
    state_dir = adapter._get_profile_state_dir()
    state_dir.mkdir(parents=True)
    if blocker in ('paused', 'stop_requested'):
        adapter.update_control_state({blocker: True})
    elif blocker == 'session_import_required':
        (state_dir / 'session_import_required.json').write_text('{}')
    elif blocker == 'auth_backoff':
        (state_dir / 'auth_backoff.json').write_text(json.dumps({'until': time.time() + 600}))
    else:
        browser = adapter._bot_profile_root() / 'browser'
        browser.mkdir()
        (browser / '.session_import.lock').touch()
    assert starter.main() == 0
    assert starts == []
    assert updates == []


def test_overdue_after_sleep_can_start_and_active_worker_is_not_duplicated(runtime, monkeypatch):
    adapter, starts, _updates = runtime
    state_dir = adapter._get_profile_state_dir()
    state_dir.mkdir(parents=True)
    (state_dir / f'last_send_{safe_name_key("target")}.txt').write_text(str(time.time() - 24 * 3600))
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=True, pid=123))
    assert starter.main() == 0
    assert starts == []
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=False, pid=None))
    assert starter.main() == 0
    assert starts == [True]


def test_failed_time_adjustment_returns_failure_instead_of_success(runtime, monkeypatch):
    adapter, starts, _updates = runtime
    state_dir = adapter._get_profile_state_dir()
    state_dir.mkdir(parents=True)
    (state_dir / f'last_send_{safe_name_key("target")}.txt').write_text(str(time.time()))
    def denied():
        raise RuntimeError('Access is denied')
    monkeypatch.setattr(adapter.scheduler, 'reanchor_from_queue', denied)
    assert starter.main() == 3
    assert starts == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows Task Scheduler trigger definitions')
def test_registration_builds_only_12_hour_trigger_without_registering_tasks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    captured = tmp_path / 'definition.json'
    script = tmp_path / 'inspect.ps1'
    script.write_text('''
$ErrorActionPreference = 'Stop'
Import-Module ScheduledTasks
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description, [switch]$Force)
    @{
        intervals = @($Trigger | ForEach-Object { $_.Repetition.Interval })
        starts = @($Trigger | ForEach-Object { $_.StartBoundary })
        catch_up = $Settings.StartWhenAvailable
        wake = $Settings.WakeToRun
        execute = $Action.Execute
        arguments = $Action.Arguments
    } | ConvertTo-Json | Set-Content -LiteralPath $env:HEART_TEST_CAPTURE -Encoding UTF8
}
function Get-ScheduledTask { return $null }
& $env:HEART_TEST_REGISTER -TaskName 'Heart isolated test' -AtStartup false -Every12Hours true -FirstRunAt '2030-01-01 12:00:00'
''', encoding='utf-8')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
        env={**os.environ, 'HEART_TEST_CAPTURE': str(captured), 'HEART_TEST_REGISTER': str(root / 'scripts/windows/register_worker_schedule.ps1')},
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    definition = json.loads(captured.read_text(encoding='utf-8-sig'))
    assert definition['intervals'] == ['PT12H']
    expected_start = time.mktime(time.strptime('2030-01-01 12:00:00', '%Y-%m-%d %H:%M:%S'))
    assert all(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() == expected_start for value in definition['starts'])
    assert definition['catch_up'] is True
    assert definition['wake'] is False
    assert definition['execute'] == 'wscript.exe'


@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
def test_launcher_does_not_reschedule_or_override_user_settings(tmp_path):
    windows = tmp_path / 'scripts/windows'
    windows.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'scripts/windows/start_worker.ps1', windows)
    (tmp_path / 'scripts/start_worker_once.py').write_text('print("[starter] worker_started pid=123")')
    (windows / 'register_worker_schedule.ps1').write_text('throw "must not re-register tasks"')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(windows / 'start_worker.ps1')],
        env={**os.environ, 'YARA_PYTHON': sys.executable},
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0
    log = (tmp_path / 'logs/worker_scheduler.log').read_text(encoding='utf-8')
    assert '[starter] worker_started pid=123' in log
    assert 'Schedule refresh' not in log


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows Task Scheduler')
def test_native_task_registration_and_hidden_launch_without_tiktok(tmp_path):
    windows = tmp_path / 'scripts/windows'
    windows.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'scripts/windows/register_worker_schedule.ps1', windows)
    (windows / 'start_worker.vbs').write_text(
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'Set output = fso.CreateTextFile(fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "ran.txt"), True)\n'
        'output.WriteLine "no_browser_no_messages"\noutput.Close\n', encoding='utf-8',
    )
    name = f'Heart isolated {uuid.uuid4().hex}'
    env = {**os.environ, 'HEART_TEST_REGISTER': str(windows / 'register_worker_schedule.ps1'), 'HEART_TEST_TASK': name}
    command = '''
$ErrorActionPreference = 'Stop'
try {
    & $env:HEART_TEST_REGISTER -TaskName $env:HEART_TEST_TASK -AtStartup false -Every12Hours true -FirstRunAt '2030-01-01 12:00:00'
    $task = Get-ScheduledTask -TaskName $env:HEART_TEST_TASK
    if (@($task.Triggers).Count -ne 1 -or $task.Triggers[0].Repetition.Interval -ne 'PT12H') { throw 'Unexpected trigger' }
    $task.Triggers[0].StartBoundary = '2030-01-02T12:00:00'
    Set-ScheduledTask -TaskName $env:HEART_TEST_TASK -Trigger $task.Triggers -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $env:HEART_TEST_TASK
    for ($index = 0; $index -lt 40; $index++) {
        Start-Sleep -Milliseconds 250
        $info = Get-ScheduledTaskInfo -TaskName $env:HEART_TEST_TASK
        if ($info.LastRunTime.Year -gt 2020 -and (Get-ScheduledTask -TaskName $env:HEART_TEST_TASK).State -ne 'Running') { break }
    }
    if ($info.LastTaskResult -ne 0) { throw "Task result: $($info.LastTaskResult)" }
}
finally {
    $task = Get-ScheduledTask -TaskName $env:HEART_TEST_TASK -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $env:HEART_TEST_TASK -Confirm:$false }
}
'''
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
        env=env, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert (windows / 'ran.txt').read_text().strip() == 'no_browser_no_messages'

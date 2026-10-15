from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import yara_app.project_adapter as project_adapter
from yara_app.project_adapter import ProjectAdapter


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def setup_project(tmp_path: Path) -> ProjectAdapter:
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    (tmp_path / 'telegram_control_bot.py').write_text('print("tg")\n', encoding='utf-8')
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})
    return ProjectAdapter(tmp_path)


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_running(self):
        return True

    def status(self):
        return 'running'

    def children(self, recursive: bool = True):  # noqa: ARG002
        del recursive
        return []

    def terminate(self):
        return None


class FakePsutil:
    STATUS_ZOMBIE = 'zombie'

    @staticmethod
    def Process(pid: int):
        return FakeProcess(pid)

    @staticmethod
    def wait_procs(procs, timeout: float):  # noqa: ARG002
        return procs, []



def test_pid_exists_prefers_psutil_without_tasklist(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    monkeypatch.setattr(project_adapter, 'psutil', FakePsutil)

    def fail_run(*args, **kwargs):  # noqa: ARG001, ARG002
        raise AssertionError('tasklist/taskkill should not be used when psutil works')

    monkeypatch.setattr(project_adapter.subprocess, 'run', fail_run)

    assert adapter._pid_exists(321) is True


def test_worker_status_ignores_saved_pid_owned_by_another_program(tmp_path: Path) -> None:
    adapter = setup_project(tmp_path)
    adapter.update_control_state({'last_run_pid': os.getpid(), 'last_run_started_at': time.time()})

    assert adapter.get_worker_status().running is False


@pytest.mark.parametrize('launch_mode', ['absolute', 'relative', 'module'])
def test_worker_status_recognizes_its_script(tmp_path: Path, monkeypatch, launch_mode: str) -> None:
    adapter = setup_project(tmp_path)
    adapter.update_control_state({'last_run_pid': 321, 'last_run_started_at': 100.0})
    args = {
        'absolute': [str(adapter.main_script_path)],
        'relative': [adapter.main_script_path.name],
        'module': ['-m', adapter.main_script_path.stem],
    }[launch_mode]
    proc = SimpleNamespace(
        create_time=lambda: 99.5,
        cmdline=lambda: [sys.executable, '-u', *args],
        cwd=lambda: str(tmp_path),
    )
    monkeypatch.setattr(adapter, '_pid_exists', lambda pid: pid == 321)
    monkeypatch.setattr(project_adapter.psutil, 'Process', lambda _pid: proc)

    assert adapter.get_worker_status().running is True
    assert adapter.get_worker_status().pid == 321


def test_worker_status_rejects_reused_pid_even_for_the_same_script(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    adapter.update_control_state({'last_run_pid': 321, 'last_run_started_at': 100.0})
    proc = SimpleNamespace(
        create_time=lambda: 200.0,
        cmdline=lambda: [sys.executable, str(adapter.main_script_path)],
        cwd=lambda: str(tmp_path),
    )
    monkeypatch.setattr(adapter, '_pid_exists', lambda pid: pid == 321)
    monkeypatch.setattr(project_adapter.psutil, 'Process', lambda _pid: proc)

    assert adapter.get_worker_status().running is False


def test_start_worker_recovers_from_a_saved_unrelated_pid(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    adapter.update_control_state({
        'last_run_pid': os.getpid(), 'last_run_started_at': time.time(), 'stop_requested': True,
    })
    launched = []

    def fake_popen(command, **_kwargs):
        assert adapter.get_control_state()['stop_requested'] is False
        launched.append(command)
        return SimpleNamespace(pid=987)

    monkeypatch.setattr(project_adapter.subprocess, 'Popen', fake_popen)

    status = adapter.start_worker()

    assert status.pid == 987
    assert len(launched) == 1
    assert str(adapter.main_script_path) in launched[0]


def test_stop_worker_never_terminates_an_unrelated_saved_pid(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    adapter.update_control_state({'last_run_pid': os.getpid(), 'last_run_started_at': time.time()})

    def fail_run(*_args, **_kwargs):
        raise AssertionError('An unrelated process must not be terminated')

    monkeypatch.setattr(project_adapter.subprocess, 'run', fail_run)

    assert adapter.stop_worker(timeout=0).running is False
    assert adapter.get_control_state()['last_run_pid'] is None

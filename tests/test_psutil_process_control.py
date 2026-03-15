from __future__ import annotations

import json
from pathlib import Path

import project_adapter
from project_adapter import ProjectAdapter


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

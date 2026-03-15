from __future__ import annotations

import json
from pathlib import Path

import project_adapter
from project_adapter import ProjectAdapter, WorkerStatus


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def setup_project(tmp_path: Path) -> ProjectAdapter:
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    (tmp_path / 'telegram_control_bot.py').write_text('print("tg")\n', encoding='utf-8')
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})
    return ProjectAdapter(tmp_path)


def test_start_worker_hides_console_on_windows(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    captured: dict[str, object] = {}

    class DummyProcess:
        pid = 501

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return DummyProcess()

    monkeypatch.setattr(project_adapter.os, 'name', 'nt', raising=False)
    monkeypatch.setattr(project_adapter.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, raising=False)
    monkeypatch.setattr(project_adapter.subprocess, 'CREATE_NO_WINDOW', 0x8000000, raising=False)
    monkeypatch.setattr(project_adapter.subprocess, 'Popen', fake_popen)

    status = adapter.start_worker()

    assert status.running is True
    assert status.pid == 501
    assert captured['kwargs']['creationflags'] == 0x200 | 0x8000000


def test_start_telegram_bot_tracks_pid_and_uses_log(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    write_json(tmp_path / 'control' / 'telegram_bot_v2.json', {'token': 'abc123'})
    captured: dict[str, object] = {}

    class DummyProcess:
        pid = 777

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return DummyProcess()

    monkeypatch.setattr(project_adapter.subprocess, 'Popen', fake_popen)

    status = adapter.start_telegram_bot()

    assert status.running is True
    assert status.pid == 777
    assert captured['cmd'][2].endswith('telegram_control_bot.py')
    state = adapter.get_control_state()
    assert state['telegram_bot_pid'] == 777
    assert Path(captured['kwargs']['stdout'].name).name == 'telegram_control_bot_v2.log'


def test_start_all_skips_telegram_when_not_ready(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    monkeypatch.setattr(ProjectAdapter, 'start_worker', lambda self: WorkerStatus(running=True, pid=321))

    result = adapter.start_all()

    assert result['worker'].pid == 321
    assert result['telegram'] is None

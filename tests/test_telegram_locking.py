from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from project_adapter import ProjectAdapter
from telegram_control_bot import TelegramControlBot


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def setup_project(tmp_path: Path) -> ProjectAdapter:
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    (tmp_path / 'telegram_control_bot.py').write_text('print("tg")\n', encoding='utf-8')
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})
    write_json(tmp_path / 'control' / 'telegram_bot_v2.json', {'token': 'abc123'})
    return ProjectAdapter(tmp_path)


def test_start_telegram_bot_reuses_live_lock_pid(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    adapter._write_pid_file(adapter.telegram_lock_path, 999)
    monkeypatch.setattr(ProjectAdapter, '_pid_exists', lambda self, pid: pid == 999)

    popen_called = {'value': False}

    def fake_popen(*args, **kwargs):  # noqa: ARG001
        popen_called['value'] = True
        raise AssertionError('Popen should not be called when lock PID is alive')

    monkeypatch.setattr('project_adapter.subprocess.Popen', fake_popen)

    status = adapter.start_telegram_bot()

    assert status.running is True
    assert status.pid == 999
    assert popen_called['value'] is False


def test_stop_telegram_bot_terminates_all_detected_instances(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    adapter.update_control_state({'telegram_bot_pid': 111, 'telegram_bot_started_at': 1.0})
    adapter._write_pid_file(adapter.telegram_lock_path, 222)

    monkeypatch.setattr(ProjectAdapter, '_discover_running_script_pids', lambda self, script, fallback=None: [111, 222])
    monkeypatch.setattr(ProjectAdapter, '_pid_exists', lambda self, pid: pid in {111, 222})

    terminated: list[int] = []

    def fake_terminate(self, pid: int, *, label: str, timeout: float = 8.0) -> None:  # noqa: ARG002
        terminated.append(pid)

    monkeypatch.setattr(ProjectAdapter, '_terminate_pid', fake_terminate)

    status = adapter.stop_telegram_bot()

    assert status.running is False
    assert terminated == [111, 222]
    assert adapter._read_pid_file(adapter.telegram_lock_path) is None
    state = adapter.get_control_state()
    assert state['telegram_bot_pid'] is None


def test_telegram_bot_refuses_duplicate_lock_owner(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path)
    adapter._write_pid_file(adapter.telegram_lock_path, 555)

    bot = TelegramControlBot(tmp_path)
    monkeypatch.setattr(bot.adapter, '_pid_exists', lambda pid: pid == 555)

    assert bot._acquire_instance_lock() is False


def test_telegram_bot_stops_on_http_409_conflict(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    bot = TelegramControlBot(tmp_path)

    released = {"value": False}

    monkeypatch.setattr(bot, '_acquire_instance_lock', lambda: True)
    monkeypatch.setattr(bot, '_restore_reply_panels', lambda: None)
    monkeypatch.setattr(bot, '_release_instance_lock', lambda: released.__setitem__("value", True))
    monkeypatch.setattr('telegram_control_bot.atexit.register', lambda fn: None)
    monkeypatch.setattr('telegram_control_bot.time.sleep', lambda seconds: None)

    error = urllib.error.HTTPError('https://api.telegram.org', 409, 'Conflict', {}, None)
    monkeypatch.setattr(bot.api, 'get_updates', lambda offset, timeout: (_ for _ in ()).throw(error))

    bot.run_forever()

    assert released['value'] is True

from __future__ import annotations

import json
from pathlib import Path

import launcher
from project_adapter import ProjectAdapter, WorkerStatus


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def test_launcher_starts_worker_without_telegram_config(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})

    started: list[bool] = []
    diagnostics: list[tuple[list[str], list[str] | None]] = []
    telegram_started: list[bool] = []

    def fake_start_worker(self: ProjectAdapter) -> WorkerStatus:
        started.append(True)
        return WorkerStatus(running=True, pid=321)

    def fake_get_worker_status(self: ProjectAdapter) -> WorkerStatus:
        return WorkerStatus(running=False, pid=None)

    class DummyBot:
        def __init__(self, *_args, **_kwargs) -> None:
            telegram_started.append(True)

        def run_forever(self) -> None:
            telegram_started.append(True)

    monkeypatch.setattr(ProjectAdapter, 'start_worker', fake_start_worker)
    monkeypatch.setattr(ProjectAdapter, 'get_worker_status', fake_get_worker_status)
    monkeypatch.setattr(launcher, 'TelegramControlBot', DummyBot)
    monkeypatch.setattr(launcher.UnifiedBotLauncher, '_show_diagnostics', lambda self, errors, warnings=None: diagnostics.append((errors, warnings)))

    app = launcher.UnifiedBotLauncher(tmp_path)
    app.run()

    assert started == [True]
    assert diagnostics == []
    assert telegram_started == []


def test_start_worker_forces_utf8_output(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})

    captured: dict[str, object] = {}

    class DummyProcess:
        pid = 987

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return DummyProcess()

    monkeypatch.setattr('subprocess.Popen', fake_popen)

    adapter = ProjectAdapter(tmp_path)
    status = adapter.start_worker()

    env = captured['kwargs']['env']
    assert env['PYTHONIOENCODING'] == 'utf-8'
    assert env['PYTHONUTF8'] == '1'
    assert status.running is True
    assert status.pid == 987


def test_tail_file_returns_last_lines_only(tmp_path: Path) -> None:
    log_path = tmp_path / 'tiktok_bot.log'
    log_path.write_text('\n'.join(f'line-{idx}' for idx in range(10)), encoding='utf-8')

    adapter = ProjectAdapter(tmp_path)
    assert adapter.tail_file(log_path, lines=3) == ['line-7', 'line-8', 'line-9']

from __future__ import annotations

import json
from pathlib import Path

from yara_app.project_adapter import ProjectAdapter


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


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
    assert captured['kwargs']['stdout'].closed is True
    assert status.running is True
    assert status.pid == 987


def test_tail_file_returns_last_lines_only(tmp_path: Path) -> None:
    log_path = tmp_path / 'tiktok_bot.log'
    log_path.write_text('\n'.join(f'line-{idx}' for idx in range(10)), encoding='utf-8')

    adapter = ProjectAdapter(tmp_path)
    assert adapter.tail_file(log_path, lines=3) == ['line-7', 'line-8', 'line-9']

from __future__ import annotations

import json
from pathlib import Path

import project_adapter
from project_adapter import ProjectAdapter


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def test_message_pool_normalization_and_stats(tmp_path: Path) -> None:
    (tmp_path / 'message_pool.txt').write_text('# comments\nhello\n\nhello\nworld\n', encoding='utf-8')
    adapter = ProjectAdapter(tmp_path)

    stats = adapter.get_message_pool_stats()
    assert stats['count'] == 2
    assert stats['comment_lines'] == 1
    assert stats['duplicate_count'] == 1

    normalized = adapter.normalize_message_pool_text('# comments\nhello\n\nhello\nworld\n')
    assert normalized == '# comments\nhello\nworld\n'


def test_diagnostics_explains_json_statuses(tmp_path: Path) -> None:
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})
    write_json(tmp_path / 'control' / 'telegram_bot_v2.json', {'token': 'abc'})
    (tmp_path / 'message_pool.txt').write_text('hello\nhello\n', encoding='utf-8')
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    (tmp_path / 'telegram_control_bot.py').write_text('print("tg")\n', encoding='utf-8')
    (tmp_path / 'BUILD_INFO.json').write_text(json.dumps({'build': 'test-build'}, ensure_ascii=False), encoding='utf-8')

    adapter = ProjectAdapter(tmp_path)
    diag = adapter.diagnostics()

    assert 'health_score' in diag
    assert 'issues' in diag
    assert 'recommendations' in diag
    assert any(item['status'] == 'JSON корректен' for item in diag['file_details'])
    assert any('Это не означает' in item['meaning'] for item in diag['file_details'] if item['kind'] == 'json')


def test_start_worker_preserves_dry_run_before_launch(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha', 'dry_run': True})
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    adapter = ProjectAdapter(tmp_path)

    class DummyProcess:
        pid = 1234

    monkeypatch.setattr(project_adapter.subprocess, 'Popen', lambda *args, **kwargs: DummyProcess())

    adapter.start_worker()

    state = adapter.get_control_state()
    assert state['dry_run'] is True


def test_reset_runtime_flags_preserves_dry_run(tmp_path: Path) -> None:
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha', 'dry_run': True, 'paused': True, 'stop_requested': True})
    adapter = ProjectAdapter(tmp_path)

    state = adapter.reset_runtime_flags()

    assert state['dry_run'] is True
    assert state['paused'] is False
    assert state['stop_requested'] is False

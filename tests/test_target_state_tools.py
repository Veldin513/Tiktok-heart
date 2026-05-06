from __future__ import annotations

import datetime
import json
from pathlib import Path

import yara_app.project_adapter as project_adapter
from yara_app.project_adapter import ProjectAdapter
from yara_app.ttbot.models import legacy_safe_filename, safe_name_key


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_reset_target_cooldown_removes_only_active_runtime_last_send_file_for_unicode_name(tmp_path: Path) -> None:
    target = 'ᵉʳ.ᵐⁱⁿᵉ'
    state_dir = tmp_path / 'profiles' / 'test_profile' / 'state'
    stable = state_dir / f'last_send_{safe_name_key(target)}.txt'
    legacy = state_dir / f'last_send_{legacy_safe_filename(target)}.txt'
    _write(stable, '123.0')
    _write(legacy, '456.0')

    adapter = ProjectAdapter(tmp_path)
    removed = adapter.reset_target_cooldown(target, 'test_profile')

    assert removed == (1, str(stable))
    assert not stable.exists()
    assert legacy.exists()


def test_reset_target_cooldown_handles_runtime_key_with_hidden_unicode(tmp_path: Path) -> None:
    target = 'er\u200b.mine'
    state_dir = tmp_path / 'profiles' / 'test_profile' / 'state'
    stable = state_dir / f'last_send_{safe_name_key(target)}.txt'
    _write(stable, '789.0')

    adapter = ProjectAdapter(tmp_path)
    removed = adapter.reset_target_cooldown(target, 'test_profile')

    assert removed == (1, str(stable))
    assert not stable.exists()


def test_set_target_streak_updates_only_active_stats_file_and_preserves_date(tmp_path: Path) -> None:
    target = 'ᵉʳ.ᵐⁱⁿᵉ'
    state_dir = tmp_path / 'profiles' / 'test_profile' / 'state'
    existing_date = '2026-03-10'
    stable = state_dir / f'stats_{safe_name_key(target)}.txt'
    legacy = state_dir / f'stats_{legacy_safe_filename(target)}.txt'
    _write(stable, f'4|{existing_date}')
    _write(legacy, f'2|2026-03-01')

    adapter = ProjectAdapter(tmp_path)
    written = adapter.set_target_streak(target, 7, 'test_profile')

    assert written == (1, str(stable))
    assert stable.read_text(encoding='utf-8') == f'7|{existing_date}'
    assert legacy.read_text(encoding='utf-8') == f'2|2026-03-01'


def test_set_target_streak_creates_runtime_files_when_missing(tmp_path: Path) -> None:
    target = 'user\u200b.name'
    state_dir = tmp_path / 'profiles' / 'test_profile' / 'state'

    adapter = ProjectAdapter(tmp_path)
    written = adapter.set_target_streak(target, 3, 'test_profile')

    stable = state_dir / f'stats_{safe_name_key(target)}.txt'
    legacy = state_dir / f'stats_{legacy_safe_filename(target)}.txt'
    today = datetime.date.today().strftime('%Y-%m-%d')
    assert written == (1, str(stable))
    assert stable.read_text(encoding='utf-8') == f'3|{today}'
    assert not legacy.exists()


def test_get_target_state_reports_existing_runtime_files(tmp_path: Path) -> None:
    target = 'ᵉʳ.ᵐⁱⁿᵉ'
    state_dir = tmp_path / 'profiles' / 'test_profile' / 'state'
    stable_send = state_dir / f'last_send_{safe_name_key(target)}.txt'
    stable_stats = state_dir / f'stats_{safe_name_key(target)}.txt'
    _write(stable_send, '123.0')
    _write(stable_stats, '5|2026-03-12')

    adapter = ProjectAdapter(tmp_path)
    state = adapter.get_target_state(target, 'test_profile')

    assert state['streak_count'] == 5
    assert stable_send.as_posix() in [p.replace('\\', '/') for p in state['state_files']['last_send']]
    assert stable_stats.as_posix() in [p.replace('\\', '/') for p in state['state_files']['stats']]


def test_get_target_state_reports_time_until_next_send(tmp_path: Path, monkeypatch) -> None:
    target = 'sample'
    state_dir = tmp_path / 'profiles' / 'test_profile' / 'state'
    stable_send = state_dir / f'last_send_{safe_name_key(target)}.txt'
    _write(stable_send, '1000')
    control_dir = tmp_path / 'control'
    control_dir.mkdir(parents=True)
    (control_dir / 'control_state.json').write_text(
        json.dumps({'active_profile': 'test_profile', 'cooldown_hours': 2}),
        encoding='utf-8',
    )
    monkeypatch.setattr(project_adapter.time, 'time', lambda: 1900)
    monkeypatch.setattr(project_adapter.time, 'localtime', lambda value: project_adapter.time.struct_time((1970, 1, 1, 2, 13, 20, 3, 1, 0)))

    state = ProjectAdapter(tmp_path).get_target_state(target, 'test_profile')

    assert state['ready'] is False
    assert state['cooldown_left_s'] == 6300
    assert state['cooldown_left_text'] == '1 ч 45 мин'
    assert state['next_send_at'] == 8200
    assert state['next_send_at_text'] == '1970-01-01 02:13:20'

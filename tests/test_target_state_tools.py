from __future__ import annotations

import datetime
from pathlib import Path

from project_adapter import ProjectAdapter
from ttbot.models import legacy_safe_filename, safe_name_key


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_reset_target_cooldown_removes_only_active_runtime_last_send_file_for_unicode_name(tmp_path: Path) -> None:
    target = 'sample_target'
    state_dir = tmp_path / 'profiles' / 'demo_profile' / 'state'
    stable = state_dir / f'last_send_{safe_name_key(target)}.txt'
    legacy = state_dir / f'last_send_{legacy_safe_filename(target)}.txt'
    _write(stable, '123.0')
    _write(legacy, '456.0')

    adapter = ProjectAdapter(tmp_path)
    removed = adapter.reset_target_cooldown(target, 'demo_profile')

    assert removed == (1, str(stable))
    assert not stable.exists()
    assert legacy.exists()


def test_reset_target_cooldown_handles_runtime_key_with_hidden_unicode(tmp_path: Path) -> None:
    target = 'er\u200b.mine'
    state_dir = tmp_path / 'profiles' / 'demo_profile' / 'state'
    stable = state_dir / f'last_send_{safe_name_key(target)}.txt'
    _write(stable, '789.0')

    adapter = ProjectAdapter(tmp_path)
    removed = adapter.reset_target_cooldown(target, 'demo_profile')

    assert removed == (1, str(stable))
    assert not stable.exists()


def test_set_target_streak_updates_only_active_stats_file_and_preserves_date(tmp_path: Path) -> None:
    target = 'sample_target'
    state_dir = tmp_path / 'profiles' / 'demo_profile' / 'state'
    existing_date = '2026-03-10'
    stable = state_dir / f'stats_{safe_name_key(target)}.txt'
    legacy = state_dir / f'stats_{legacy_safe_filename(target)}.txt'
    _write(stable, f'4|{existing_date}')
    _write(legacy, f'2|2026-03-01')

    adapter = ProjectAdapter(tmp_path)
    written = adapter.set_target_streak(target, 7, 'demo_profile')

    assert written == (1, str(stable))
    assert stable.read_text(encoding='utf-8') == f'7|{existing_date}'
    assert legacy.read_text(encoding='utf-8') == f'2|2026-03-01'


def test_set_target_streak_creates_runtime_files_when_missing(tmp_path: Path) -> None:
    target = 'user\u200b.name'
    state_dir = tmp_path / 'profiles' / 'demo_profile' / 'state'

    adapter = ProjectAdapter(tmp_path)
    written = adapter.set_target_streak(target, 3, 'demo_profile')

    stable = state_dir / f'stats_{safe_name_key(target)}.txt'
    legacy = state_dir / f'stats_{legacy_safe_filename(target)}.txt'
    today = datetime.date.today().strftime('%Y-%m-%d')
    assert written == (1, str(stable))
    assert stable.read_text(encoding='utf-8') == f'3|{today}'
    assert not legacy.exists()


def test_get_target_state_reports_existing_runtime_files(tmp_path: Path) -> None:
    target = 'sample_target'
    state_dir = tmp_path / 'profiles' / 'demo_profile' / 'state'
    stable_send = state_dir / f'last_send_{safe_name_key(target)}.txt'
    stable_stats = state_dir / f'stats_{safe_name_key(target)}.txt'
    _write(stable_send, '123.0')
    _write(stable_stats, '5|2026-03-12')

    adapter = ProjectAdapter(tmp_path)
    state = adapter.get_target_state(target, 'demo_profile')

    assert state['streak_count'] == 5
    assert stable_send.as_posix() in [p.replace('\\', '/') for p in state['state_files']['last_send']]
    assert stable_stats.as_posix() in [p.replace('\\', '/') for p in state['state_files']['stats']]

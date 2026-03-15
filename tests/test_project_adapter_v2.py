from __future__ import annotations

import json
from pathlib import Path

from project_adapter import ProjectAdapter


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_profiles_dict_structure(tmp_path: Path) -> None:
    write_json(
        tmp_path / "control" / "profiles.json",
        {
            "alpha": {"name": "Alpha", "enabled": True},
            "beta": {"name": "Beta", "enabled": False},
        },
    )
    adapter = ProjectAdapter(tmp_path)
    profiles = adapter.get_profiles()
    assert len(profiles) == 2
    assert profiles[0].label == "Alpha"
    assert profiles[1].enabled is False


def test_toggle_profile_in_list(tmp_path: Path) -> None:
    write_json(
        tmp_path / "control" / "profiles.json",
        [
            {"name": "One", "enabled": True},
            {"name": "Two", "enabled": False},
        ],
    )
    adapter = ProjectAdapter(tmp_path)
    updated = adapter.toggle_profile(1)
    assert updated.enabled is True


def test_message_pool_stats(tmp_path: Path) -> None:
    (tmp_path / "message_pool.txt").write_text("hello\n\nworld\n", encoding="utf-8")
    adapter = ProjectAdapter(tmp_path)
    stats = adapter.get_message_pool_stats()
    assert stats["count"] == 2



def test_message_pool_raw_save_and_backup(tmp_path: Path) -> None:
    adapter = ProjectAdapter(tmp_path)
    adapter.save_message_pool_text_raw('hello\nhello\n# keep\n')
    assert (tmp_path / 'message_pool.txt').read_text(encoding='utf-8') == 'hello\nhello\n# keep\n'
    backup = adapter.create_message_pool_backup('manual\nbackup\n')
    assert backup.exists()
    assert backup.read_text(encoding='utf-8') == 'manual\nbackup\n'


def test_current_run_snapshot_reads_active_profile_artifacts(tmp_path: Path) -> None:
    write_json(tmp_path / 'control' / 'profiles.json', {'alpha': []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': 'alpha'})
    artifacts = tmp_path / 'profiles' / 'alpha' / 'artifacts'
    artifacts.mkdir(parents=True)
    write_json(tmp_path / 'tiktok_checker.py', {'noop': True})
    write_json(artifacts / 'run_state.json', {'status': 'running', 'current_target': 'alice', 'total_targets': 3})
    write_json(artifacts / 'run_summary.json', {
        'profile_name': 'alpha',
        'total_targets': 3,
        'success_count': 1,
        'skipped_count': 1,
        'failed_count': 0,
        'duration_seconds': 42,
        'results': [],
    })

    adapter = ProjectAdapter(tmp_path)
    snapshot = adapter.get_current_run_snapshot()

    assert snapshot['status'] == 'running'
    assert snapshot['current_target'] == 'alice'
    assert snapshot['success_count'] == 1
    assert snapshot['total_targets'] == 3

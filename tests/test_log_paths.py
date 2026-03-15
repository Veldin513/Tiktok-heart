from __future__ import annotations

import json
from pathlib import Path

import project_adapter
from project_adapter import ProjectAdapter


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def setup_project(tmp_path: Path, profile: str = 'demo_profile') -> ProjectAdapter:
    (tmp_path / 'tiktok_checker.py').write_text('print("ok")\n', encoding='utf-8')
    (tmp_path / 'telegram_control_bot.py').write_text('print("tg")\n', encoding='utf-8')
    write_json(tmp_path / 'control' / 'profiles.json', {profile: []})
    write_json(tmp_path / 'control' / 'control_state.json', {'active_profile': profile})
    return ProjectAdapter(tmp_path)


def test_profile_worker_logs_live_under_profiles_logs(tmp_path: Path, monkeypatch) -> None:
    adapter = setup_project(tmp_path, 'alpha')
    captured: dict[str, object] = {}

    class DummyProcess:
        pid = 321

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return DummyProcess()

    monkeypatch.setattr(project_adapter.subprocess, 'Popen', fake_popen)

    adapter.start_worker()

    stdout_name = Path(captured['kwargs']['stdout'].name)
    assert stdout_name.name == 'tiktok_worker.out.log'
    assert stdout_name.parent == tmp_path / 'profiles' / 'alpha' / 'logs'


def test_legacy_root_logs_are_migrated_to_runtime_log_folders(tmp_path: Path) -> None:
    adapter = setup_project(tmp_path, 'alpha')
    legacy_worker = tmp_path / 'tiktok_worker.out.log'
    legacy_launcher = tmp_path / 'launcher.log'
    legacy_worker.write_text('worker legacy\n', encoding='utf-8')
    legacy_launcher.write_text('launcher legacy\n', encoding='utf-8')

    adapter.ensure_runtime_files()

    assert not legacy_worker.exists()
    assert not legacy_launcher.exists()
    assert (tmp_path / 'profiles' / 'alpha' / 'logs' / 'tiktok_worker.out.log').read_text(encoding='utf-8') == 'worker legacy\n'
    assert (tmp_path / 'logs' / 'launcher.log').read_text(encoding='utf-8') == 'launcher legacy\n'

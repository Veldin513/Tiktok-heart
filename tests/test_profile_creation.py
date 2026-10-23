import json

import pytest

from app_shell.server import AppShellHandler
from yara_app.project_adapter import ProjectAdapter, WorkerStatus


def make_adapter(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(False, None))
    return adapter


def test_profile_and_target_api_save_only_selected_profile(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    before = json.loads(adapter.profiles_path.read_text())
    handler = object.__new__(AppShellHandler)
    handler.adapter = adapter
    results = []
    handler._send_json = results.append
    handler._handle_profile_action({'action': 'create', 'profile_key': 'second'})
    handler._handle_target_action({
        'action': 'add', 'profile_key': 'second', 'target_name': 'Friend', 'profile_url': '@example_test',
    })
    saved = json.loads(adapter.profiles_path.read_text())
    assert all(saved[key] == value for key, value in before.items())
    assert saved['second'] == [{'name': 'Friend', 'url': 'https://www.tiktok.com/@example_test'}]
    assert all(result['ok'] for result in results)
    assert adapter.get_control_state()['active_profile'] != 'second'
    with pytest.raises(ValueError, match='уже'):
        adapter.create_profile('SECOND')
    with pytest.raises(ValueError, match='уже'):
        adapter.add_target('second', 'Other name', '@example_test')


@pytest.mark.parametrize('key', ['../escape', 'CON', 'LPT1', 'bad/name', '', 'a' * 49])
def test_profile_name_cannot_escape_or_break_windows_paths(tmp_path, monkeypatch, key):
    adapter = make_adapter(tmp_path, monkeypatch)
    before = adapter.profiles_path.read_bytes()
    with pytest.raises(ValueError):
        adapter.create_profile(key)
    assert adapter.profiles_path.read_bytes() == before


def test_target_rejects_external_url_and_running_worker(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    adapter.create_profile('second')
    with pytest.raises(ValueError):
        adapter.add_target('second', 'Friend', 'https://www.tiktok.com.evil.example/@test')
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(True, 123))
    with pytest.raises(RuntimeError):
        adapter.add_target('second', 'Friend', '@example_test')
    assert json.loads(adapter.profiles_path.read_text())['second'] == []

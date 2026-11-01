from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from yara_app.fileio import read_json
from yara_app.project_adapter import ProjectAdapter
from yara_app.ttbot import models


@pytest.fixture
def adapter(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.profiles_path.write_text(json.dumps({'alpha': [{'name': 'alice'}],
        'beta': {'targets': [{'name': 'bob'}], 'future_setting': 42}}))
    adapter.message_pool_path.write_text('Shared one\nShared two\n', encoding='utf-8')
    return adapter


def test_profile_text_reaches_worker_without_changing_other_profiles(adapter, monkeypatch):
    original_pool = adapter.message_pool_path.read_bytes()
    beta = read_json(adapter.profiles_path)['beta']
    current = adapter.get_profile_messages('alpha')
    adapter.save_profile_messages('alpha', 'Personal\nPersonal\nSecond', expected_revision=current['revision'])
    assert adapter.get_profile_messages('alpha')['text'] == 'Personal\nSecond'
    assert adapter.get_profile_messages('beta')['text'] == 'Shared one\nShared two'
    assert adapter.message_pool_path.read_bytes() == original_pool
    assert read_json(adapter.profiles_path)['beta'] == beta
    monkeypatch.setattr(models, 'PROFILES_FILE', adapter.profiles_path)
    monkeypatch.setattr(models, 'MESSAGE_POOL_FILE', adapter.message_pool_path)
    store = object.__new__(models.ControlStore)
    assert store.load_messages('alpha') == ['Personal', 'Second']
    assert store.load_messages('beta') == ['Shared one', 'Shared two']
    assert store.load_profiles()['alpha'] == [{'name': 'alice'}]


def test_stale_edit_cannot_overwrite_new_text_and_restore_shared_is_explicit(adapter):
    old = adapter.get_profile_messages('beta')['revision']
    fresh = adapter.save_profile_messages('beta', 'New text', expected_revision=old)
    with pytest.raises(ValueError, match='уже изменены'):
        adapter.save_profile_messages('beta', 'Stale text', expected_revision=old)
    adapter.save_profile_messages('beta', use_shared=True, expected_revision=fresh['revision'])
    assert adapter.get_profile_messages('beta')['source'] == 'shared'
    assert read_json(adapter.profiles_path)['beta']['future_setting'] == 42


def test_running_profile_cannot_be_edited(adapter, monkeypatch):
    original = adapter.profiles_path.read_bytes()
    monkeypatch.setattr(adapter, 'get_profile_worker_status', lambda key: SimpleNamespace(running=True))
    with pytest.raises(RuntimeError):
        adapter.save_profile_messages('alpha', 'Changed')
    assert adapter.profiles_path.read_bytes() == original


def test_remove_checks_exact_recipient_and_preserves_history(adapter):
    history = adapter._bot_profile_root('alpha') / 'artifacts/run_history.jsonl'
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text('{"event":"run_finished"}\n')
    with pytest.raises(ValueError, match='уже изменён'):
        adapter.remove_target('alpha', 0, expected_name='wrong')
    adapter.remove_target('alpha', 0, expected_name='alice')
    assert read_json(adapter.profiles_path)['alpha']['targets'] == []
    assert history.read_text() == '{"event":"run_finished"}\n'


def test_profile_message_change_cancels_only_its_waiting_job(adapter):
    queue = adapter.worker_queue()
    profiles = {p.key: p for p in adapter.get_profiles()}
    queue.enqueue_locked(profiles['alpha'])
    queue.enqueue_locked(profiles['beta'])
    adapter.save_profile_messages('alpha', 'Different text')
    assert [job['profile'] for job in queue.read()['waiting']] == ['beta']

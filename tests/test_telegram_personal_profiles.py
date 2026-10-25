from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from yara_app.notifications import notify_worker_result
from yara_app.project_adapter import ProjectAdapter, WorkerStatus
from yara_app.telegram_control_bot import TelegramControlBot
from yara_app.telegram_profiles import profile_token
from yara_app.telegram_setup import TelegramSetup
from yara_app.ttbot import models

TOKEN = '123456789:' + 'test_key_' * 5


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {'message_id': len(self.messages)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return self.send_message(chat_id, text, reply_markup)

    def answer_callback(self, query_id, text=None):
        self.answers.append((query_id, text))


def message(user_id, text, username='person'):
    return {'message': {'chat': {'id': user_id, 'type': 'private'},
                        'from': {'id': user_id, 'username': username}, 'text': text}}


def callback(user_id, data):
    return {'callback_query': {'id': 'query', 'data': data,
            'message': {'chat': {'id': user_id, 'type': 'private'}, 'message_id': 1}}}


@pytest.fixture
def bot(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.telegram_config_path.write_text(json.dumps({'token': TOKEN, 'allowed_chat_ids': [100, 200, 300],
                                                       'admin_chat_ids': [100]}))
    bot = TelegramControlBot(tmp_path)
    bot.api = FakeAPI()
    bot.process_update(message(200, '/start', 'alice'))
    bot.process_update(message(300, '/start', 'bob'))
    return bot


def owned_key(bot, user_id):
    return next(key for key, ids in bot.settings.profile_owners.items() if user_id in ids)


def add_recipient(bot, user_id, username):
    key = owned_key(bot, user_id)
    bot.process_update(callback(user_id, f'p:{profile_token(key)}:target:0'))
    bot.process_update(message(user_id, '@' + username))
    return key


def test_each_allowed_user_gets_one_profile_with_id_and_nick(bot):
    active = bot.adapter.get_control_state()['active_profile']
    alice = owned_key(bot, 200)
    bob = owned_key(bot, 300)
    assert alice != bob
    profiles = bot.adapter.get_profiles()
    assert '@alice' in next(p.label for p in profiles if p.key == alice)
    assert '200' in alice and '300' in bob
    bot.process_update(message(200, '/start', 'new_nick'))
    assert len(bot.adapter.get_profiles()) == len(profiles)
    assert owned_key(bot, 200) == alice
    assert bot.adapter.get_control_state()['active_profile'] == active


def test_unapproved_user_gets_id_without_profile_or_authorization(bot):
    before = bot.adapter.profiles_path.read_bytes()
    bot.process_update(message(400, '/start', 'stranger'))
    assert bot.adapter.profiles_path.read_bytes() == before
    assert not bot._authorized(400)
    assert '400' in bot.api.messages[-1][1]


@pytest.mark.parametrize('action', ['open', 'login', 'verify', 'cancel_login', 'start', 'stop', 'target:0', 'streak:0'])
def test_user_cannot_use_any_foreign_profile_button(bot, monkeypatch, action):
    key = owned_key(bot, 300)
    for method in ['begin_dedicated_login', 'finish_dedicated_login', 'cancel_dedicated_login',
                   'start_profile_worker', 'stop_profile_worker', 'add_target', 'set_target_streak']:
        monkeypatch.setattr(bot.adapter, method, lambda *_a, **_k: pytest.fail('foreign action executed'))
    before = bot.adapter.profiles_path.read_bytes()
    bot.process_update(callback(200, f'p:{profile_token(key)}:{action}'))
    assert bot.adapter.profiles_path.read_bytes() == before
    assert 'недоступен' in bot.api.messages[-1][1]


def test_only_own_recipient_and_streak_are_changed(bot):
    alice = add_recipient(bot, 200, 'alice_target')
    bob = add_recipient(bot, 300, 'bob_target')
    profiles = {p.key: p for p in bot.adapter.get_profiles()}
    assert profiles[alice].raw['targets'][0]['url'].endswith('/@alice_target')
    assert profiles[bob].raw['targets'][0]['url'].endswith('/@bob_target')
    bot.process_update(callback(200, f'p:{profile_token(alice)}:streak:0'))
    bot.process_update(message(200, '321'))
    assert bot.adapter.get_target_state('alice_target', alice)['streak_count'] == 321
    assert bot.adapter.get_target_state('bob_target', bob)['streak_count'] == 0


@pytest.mark.parametrize('operation', ['login', 'verify', 'cancel_login', 'start', 'stop'])
def test_own_profile_operations_keep_exact_profile_key(bot, monkeypatch, operation):
    key = owned_key(bot, 200)
    method = {'login': 'begin_dedicated_login', 'verify': 'finish_dedicated_login',
              'cancel_login': 'cancel_dedicated_login', 'start': 'start_profile_worker',
              'stop': 'stop_profile_worker'}[operation]
    calls = []
    def action(profile):
        calls.append(profile)
        return SimpleNamespace(running=True) if operation in {'start', 'stop'} else {'ok': True}
    monkeypatch.setattr(bot.adapter, method, action)
    bot.process_update(callback(200, f'p:{profile_token(key)}:{operation}'))
    assert calls == [key]


def test_old_global_control_button_is_denied_to_regular_user(bot, monkeypatch):
    monkeypatch.setattr(bot.adapter, 'start_worker', lambda: pytest.fail('global worker must not start'))
    bot.process_update(callback(200, 'act:worker:toggle'))
    assert 'администратор' in bot.api.messages[-1][1]


def test_admin_can_control_foreign_profile_and_manage_roles(bot, monkeypatch):
    key = owned_key(bot, 300)
    calls = []
    monkeypatch.setattr(bot.adapter, 'start_profile_worker', lambda p: calls.append(p) or SimpleNamespace(running=True))
    bot.process_update(callback(100, f'p:{profile_token(key)}:start'))
    assert calls == [key]
    bot.process_update(message(100, '/user 400', 'admin'))
    bot.process_update(message(400, '/start', 'new_user'))
    assert owned_key(bot, 400)
    bot.process_update(message(100, '/admin 400', 'admin'))
    assert bot._is_admin(400)


def test_regular_user_cannot_grant_roles(bot):
    before = bot.adapter.telegram_config_path.read_bytes()
    bot.process_update(message(200, '/admin 200'))
    assert not bot._is_admin(200)
    config = json.loads(bot.adapter.telegram_config_path.read_text())
    assert config['admin_chat_ids'] == [100]
    assert json.loads(before)['allowed_chat_ids'] == config['allowed_chat_ids']


def test_last_admin_cannot_be_removed(bot):
    bot.process_update(message(100, '/revoke 100', 'admin'))
    assert bot._is_admin(100)
    assert 'последнего' in bot.api.messages[-1][1]


def test_revoked_user_cannot_finish_old_pending_edit(bot):
    key = owned_key(bot, 200)
    bot.process_update(callback(200, f'p:{profile_token(key)}:target:0'))
    TelegramSetup(bot.adapter).change_access(100, 200, 'revoke')
    bot.process_update(message(200, '@unwanted'))
    assert not next(p for p in bot.adapter.get_profiles() if p.key == key).raw['targets']


def test_worker_reads_structured_personal_profiles(bot, monkeypatch):
    key = add_recipient(bot, 200, 'target')
    monkeypatch.setattr(models, 'PROFILES_FILE', bot.adapter.profiles_path)
    result = object.__new__(models.ControlStore).load_profiles()
    assert result[key] == [{'name': 'target', 'url': 'https://www.tiktok.com/@target'}]


def test_profile_worker_does_not_switch_global_profile_or_stop_other_worker(bot, monkeypatch):
    key = owned_key(bot, 200)
    bob = owned_key(bot, 300)
    adapter = bot.adapter
    active = adapter.get_control_state()['active_profile']
    adapter.main_script_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.main_script_path.write_text('# fake worker')
    calls = []
    monkeypatch.setattr(adapter, '_launch_python_process', lambda **kw: calls.append(kw) or WorkerStatus(running=True, pid=123))
    adapter.start_profile_worker(key)
    assert calls[0]['extra_args'] == [key]
    assert adapter.get_control_state()['active_profile'] == active
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=True, pid=456))
    monkeypatch.setattr(adapter, '_running_worker_profile', lambda _s: bob)
    monkeypatch.setattr(adapter, 'stop_worker', lambda: pytest.fail('foreign worker stopped'))
    with pytest.raises(RuntimeError):
        adapter.stop_profile_worker(key)
    with pytest.raises(RuntimeError):
        adapter.start_profile_worker(key)


def test_reports_go_to_admin_and_own_user_only(bot, monkeypatch):
    monkeypatch.delenv('TG_DISABLE_NOTIFICATIONS', raising=False)
    calls = []
    monkeypatch.setattr(TelegramSetup, '_call', lambda self, method, payload: calls.append(payload['chat_id']) or {'message_id': 1})
    notify_worker_result(bot.adapter, {'profile_name': owned_key(bot, 200), 'results': []}, status='failed', reason='offline')
    assert calls == [100, 200]


def test_bot_accepts_its_own_windows_python_launcher(bot, monkeypatch):
    adapter = bot.adapter
    adapter._write_pid_file(adapter.telegram_lock_path, 777)
    monkeypatch.setattr(os, 'getppid', lambda: 777)
    monkeypatch.setattr(adapter, '_pid_exists', lambda _pid: True)
    monkeypatch.setattr(adapter, '_pid_matches_script', lambda pid, script: pid == 777)
    assert bot._acquire_instance_lock()
    bot._release_instance_lock()


def test_status_does_not_overwrite_child_owned_pid_file(bot, monkeypatch):
    adapter = bot.adapter
    adapter.update_control_state({'telegram_bot_pid': 111, 'telegram_bot_started_at': 1})
    adapter._write_pid_file(adapter.telegram_lock_path, 222)
    monkeypatch.setattr(adapter, '_pid_exists', lambda pid: pid in {111, 222})
    monkeypatch.setattr(adapter, '_discover_running_script_pid', lambda *_a: 222)
    assert adapter.get_telegram_bot_status().running
    assert adapter._read_pid_file(adapter.telegram_lock_path) == 222


def test_merge_into_default_preserves_targets_and_state_and_does_not_recreate_duplicate(bot):
    adapter = bot.adapter
    data = json.loads(adapter.profiles_path.read_text())
    data['default'] = [{'name': 'existing', 'url': 'https://www.tiktok.com/@existing'}]
    adapter.profiles_path.write_text(json.dumps(data))
    state = adapter._get_profile_state_dir('default')
    state.mkdir(parents=True, exist_ok=True)
    saved = state / 'stats_existing.txt'
    saved.write_text('317|2026-10-25')
    previous_key = owned_key(bot, 200)
    TelegramSetup(adapter).bind_existing_profile(200, 'default', 'alice')
    bot.process_update(message(200, '/start', 'alice'))
    profiles = {p.key: p for p in adapter.get_profiles()}
    assert previous_key not in profiles
    assert profiles['default'].raw['targets'] == [{'name': 'existing', 'url': 'https://www.tiktok.com/@existing'}]
    assert profiles['default'].raw['source']['telegram_owner_id'] == 200
    assert 'default' in profiles['default'].label
    assert saved.read_text() == '317|2026-10-25'
    assert owned_key(bot, 200) == 'default'


def test_nonempty_personal_profile_is_not_silently_merged(bot):
    key = add_recipient(bot, 200, 'keep_me')
    before = bot.adapter.profiles_path.read_bytes()
    with pytest.raises(ValueError):
        TelegramSetup(bot.adapter).bind_existing_profile(200, 'default', 'alice')
    assert bot.adapter.profiles_path.read_bytes() == before
    assert key in {p.key for p in bot.adapter.get_profiles()}


def test_startup_menu_is_coalesced_and_old_actions_are_not_replayed(bot, monkeypatch):
    before = len(bot.api.messages)
    monkeypatch.setattr(bot.adapter, 'start_worker', lambda: pytest.fail('old worker command replayed'))
    updates = []
    for index, data in enumerate(['nav:status', 'act:worker:toggle', 'nav:control'], start=1):
        item = callback(200, data)
        item['update_id'] = index
        updates.append(item)
    bot._consume_startup_updates(updates)
    assert len(bot.api.messages) == before + 1
    assert bot.offset == 4


def test_removing_user_in_desktop_also_revokes_profile_binding(bot, monkeypatch):
    adapter = bot.adapter
    monkeypatch.setattr(adapter, 'get_telegram_bot_status', lambda: WorkerStatus(running=False, pid=None))
    TelegramSetup(adapter).save({'admin_chat_ids': [100], 'allowed_chat_ids': [300]})
    config = json.loads(adapter.telegram_config_path.read_text())
    assert 200 not in config['allowed_chat_ids']
    assert all(200 not in ids for ids in config['profile_owners'].values())


def test_disabled_personal_profile_has_no_worker_targets(bot, monkeypatch):
    key = add_recipient(bot, 200, 'target')
    adapter = bot.adapter
    adapter.toggle_profile_key(key)
    monkeypatch.setattr(models, 'PROFILES_FILE', adapter.profiles_path)
    assert object.__new__(models.ControlStore).load_profiles()[key] == []
    with pytest.raises(ValueError):
        adapter.start_profile_worker(key)

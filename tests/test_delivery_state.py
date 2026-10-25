from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import yara_app.tiktok_checker as checker
from yara_app.delivery import DeliveryRejected, DeliveryUnconfirmed, SendNotAttempted, SendReceipt
from yara_app.project_adapter import ProjectAdapter
from yara_app.ttbot.models import RunSummary, Target, safe_name_key


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    writes = []
    notifications = []
    monkeypatch.setattr(checker, 'PATHS', SimpleNamespace(state_dir=state))
    monkeypatch.setattr(checker, 'TARGET_GUARD_DIR', state / 'target_send_guards')
    monkeypatch.setattr(checker, '_write_run_state', lambda **_k: None)
    monkeypatch.setattr(checker, '_append_run_history', lambda *_a, **_k: None)
    monkeypatch.setattr(checker, 'timed_phase', lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(checker, 'open_messages', lambda _p: None)
    monkeypatch.setattr(checker, 'require_work_session', lambda _p: None)
    monkeypatch.setattr(checker, 'open_chat_by_list', lambda *_a: True)
    monkeypatch.setattr(checker, 'explicitly_logged_out', lambda _p: False)
    monkeypatch.setattr(checker, 'control_store', SimpleNamespace(load_state=lambda: SimpleNamespace(dry_run=False)))
    monkeypatch.setattr(checker, 'message_selector', SimpleNamespace(next=lambda: 'hello'))
    monkeypatch.setattr(checker.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(checker, 'store', SimpleNamespace(
        get_cooldown_status=lambda _n: SimpleNamespace(allowed=True),
        mark_sent_now=lambda _n: writes.append('cooldown'),
        update_streak_stats=lambda _n: writes.append('streak') or SimpleNamespace(current_count=1, is_new_day=True),
        get_streak_count=lambda _n: 0,
    ))
    monkeypatch.setattr(checker, 'tg', SimpleNamespace(send_target_success=lambda **_k: notifications.append('success')))
    return writes, notifications


@pytest.mark.parametrize('outcome', [None, DeliveryUnconfirmed('no ack'), DeliveryRejected('rejected')])
def test_no_receipt_cannot_advance_timer_streak_or_success(dispatch, monkeypatch, outcome):
    writes, notifications = dispatch
    def send(*_a):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(checker, 'send_message', send)
    summary = RunSummary('test', 1)
    checker.process_target(object(), Target('alice'), summary)
    assert writes == notifications == []
    assert summary.success_count == 0
    assert summary.failed_count == 1


def test_real_receipt_is_required_before_advancing_state(dispatch, monkeypatch):
    writes, notifications = dispatch
    monkeypatch.setattr(checker, 'send_message', lambda *_a: SendReceipt('123456', 'client-1', 'websocket'))
    summary = RunSummary('test', 1)
    checker.process_target(object(), Target('alice'), summary)
    assert writes == ['cooldown', 'streak']
    assert notifications == ['success']
    assert summary.to_dict()['results'][0]['confirmation'] == 'server_ack'


def test_unknown_send_stays_blocked_after_guard_ttl_and_can_be_reviewed(dispatch, tmp_path, monkeypatch):
    monkeypatch.setattr(checker, 'send_message', lambda *_a: None)
    checker.process_target(object(), Target('alice'), RunSummary('test', 1))
    path = checker.target_guard_path('alice')
    data = json.loads(path.read_text())
    data['started_at'] = 1
    path.write_text(json.dumps(data))
    assert checker.acquire_target_send_guard('alice')[0] is False
    summary = RunSummary('test', 1)
    assert checker.ready_targets_for_run([Target('alice')], summary) == []
    assert summary.results[0].reason == 'delivery_unconfirmed'


def test_adapter_exposes_unknown_delivery_and_explicit_review_clears_only_guard(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    state = adapter._get_profile_state_dir('alpha')
    guard = state / 'target_send_guards' / f'{safe_name_key("alice")}.json'
    guard.parent.mkdir(parents=True)
    guard.write_text(json.dumps({'status': 'unconfirmed'}))
    assert adapter.get_target_state('alice', 'alpha')['delivery_unconfirmed'] is True
    assert adapter.get_target_state('alice', 'alpha')['ready'] is False
    assert adapter.reset_target_cooldown('alice', 'alpha') == (1, str(guard))
    assert not guard.exists()


def test_dry_run_does_not_count_as_a_sent_message(dispatch, monkeypatch):
    monkeypatch.setattr(checker, 'control_store', SimpleNamespace(load_state=lambda: SimpleNamespace(dry_run=True)))
    monkeypatch.setattr(checker, 'send_message', lambda *_a: pytest.fail('dry run must not send'))
    summary = RunSummary('test', 1)
    checker.process_target(object(), Target('alice'), summary)
    assert summary.success_count == 0
    assert summary.skipped_count == 1


def test_dm_list_receives_unique_identity_before_profile_fallback(dispatch, monkeypatch):
    checked = []
    monkeypatch.setattr(checker, 'open_chat_by_list', lambda _page, name, url: checked.append((name, url)) or True)
    monkeypatch.setattr(checker, 'open_chat_by_profile', lambda *_a: pytest.fail('verified DM list must be preferred'))
    monkeypatch.setattr(checker, 'send_message', lambda *_a: SendReceipt('123', 'client-1', 'websocket'))
    checker.process_target(object(), Target('alice', 'https://www.tiktok.com/@unique-alice'), RunSummary('test', 1))
    assert checked == [('alice', 'https://www.tiktok.com/@unique-alice')]


def test_unverified_dm_list_falls_back_to_configured_profile(dispatch, monkeypatch):
    monkeypatch.setattr(checker, 'open_chat_by_list', lambda *_a: False)
    opened = []
    monkeypatch.setattr(checker, 'open_chat_by_profile', lambda _page, url: opened.append(url) or True)
    monkeypatch.setattr(checker, 'send_message', lambda *_a: SendReceipt('123', 'client-1', 'websocket'))
    checker.process_target(object(), Target('alice', 'https://www.tiktok.com/@unique-alice'), RunSummary('test', 1))
    assert opened == ['https://www.tiktok.com/@unique-alice']


def test_blocked_chat_does_not_send_or_navigate_around_modal(dispatch, monkeypatch):
    def blocked(*_a):
        raise SendNotAttempted('TikTok modal requires manual action')
    monkeypatch.setattr(checker, 'open_chat_by_list', blocked)
    monkeypatch.setattr(checker, 'open_chat_by_profile', lambda *_a: pytest.fail('do not navigate around a blocking modal'))
    monkeypatch.setattr(checker, 'send_message', lambda *_a: pytest.fail('blocked chat must not send'))
    summary = RunSummary('test', 1)
    checker.process_target(object(), Target('alice', 'https://www.tiktok.com/@alice'), summary)
    assert summary.results[0].reason == 'chat_blocked'


@pytest.mark.parametrize('exception', [checker.AuthSessionUnavailable, checker.AuthCheckInconclusive])
def test_auth_error_is_not_hidden_as_a_missing_chat(dispatch, monkeypatch, exception):
    def fail(*_args):
        raise exception('session check failed')
    monkeypatch.setattr(checker, 'open_chat_by_list', fail)
    with pytest.raises(exception):
        checker.process_target(object(), Target('alice'), RunSummary('test', 1))


def test_unexpected_send_error_remains_blocked_after_restart(dispatch, monkeypatch):
    def fail(*_args):
        raise RuntimeError('connection closed after click')
    monkeypatch.setattr(checker, 'send_message', fail)
    checker.process_target(object(), Target('alice'), RunSummary('test', 1))
    assert checker.target_delivery_unconfirmed('alice')
    assert not checker.acquire_target_send_guard('alice')[0]


def test_confirmed_send_with_failed_cooldown_write_keeps_no_retry_guard(dispatch, monkeypatch):
    monkeypatch.setattr(checker, 'send_message', lambda *_a: SendReceipt('123', 'client-1', 'websocket'))
    def fail(*_args):
        raise PermissionError('disk busy')
    monkeypatch.setattr(checker.store, 'mark_sent_now', fail)
    summary = RunSummary('test', 1)
    checker.process_target(object(), Target('alice'), summary)
    assert summary.success_count == 1 and summary.failed_count == 0
    assert summary.results[0].reason == 'local_state_write_failed'
    assert checker.target_delivery_unconfirmed('alice')


def test_telegram_notification_error_does_not_change_confirmed_delivery(dispatch, monkeypatch):
    monkeypatch.setattr(checker, 'send_message', lambda *_a: SendReceipt('123', 'client-1', 'websocket'))
    def fail(**_kwargs):
        raise OSError('Telegram unavailable')
    monkeypatch.setattr(checker.tg, 'send_target_success', fail)
    summary = RunSummary('test', 1)
    checker.process_target(object(), Target('alice'), summary)
    assert summary.success_count == 1 and summary.failed_count == 0

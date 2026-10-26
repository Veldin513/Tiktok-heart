from __future__ import annotations

import json
import ssl
import urllib.error
from types import SimpleNamespace

import pytest

from yara_app.https import verified_context
from yara_app.notifications import notify_worker_result, report_text
from yara_app.telegram_control_bot import TelegramAPI
from yara_app.telegram_setup import TelegramSetup

TOKEN = '123456789:' + 'safe_test_value_' * 3


def test_https_context_keeps_hostname_and_certificate_verification():
    context = verified_context()
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()['x509_ca'] > 0


@pytest.mark.parametrize('client', ['setup', 'controller'])
def test_both_telegram_clients_use_verified_context(tmp_path, monkeypatch, client):
    captured = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *_args):
            return b'{"ok":true,"result":{"username":"test_bot"}}'
    def open_request(request, *, timeout, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert timeout > 0
        captured.append(request)
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', open_request)
    if client == 'setup':
        setup = TelegramSetup(SimpleNamespace(telegram_config_path=tmp_path / 'config.json'))
        assert setup.check(TOKEN)['username'] == 'test_bot'
    else:
        assert TelegramAPI(TOKEN, 5).call('getMe')['username'] == 'test_bot'
    assert len(captured) == 1


def test_certificate_failure_is_reported_without_retrying_insecurely(tmp_path, monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise urllib.error.URLError(ssl.SSLCertVerificationError('invalid certificate'))
    monkeypatch.setattr('urllib.request.urlopen', fail)
    setup = TelegramSetup(SimpleNamespace(telegram_config_path=tmp_path / 'config.json'))
    with pytest.raises(ValueError, match='сертификат'):
        setup.check(TOKEN)
    assert len(calls) == 1 and calls[0]['context'].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(('summary', 'status', 'reason', 'expected'), [
    ({'success_count': 1, 'results': [{'target': 'alice', 'success': True, 'confirmation': 'server_ack'}]},
     'success', '', '✅ TikTok подтвердил отправку'),
    ({'success_count': 1, 'results': []}, 'success', '', '⚠️ Результат отправки требует проверки'),
    ({'results': []}, 'failed', 'session_import_required', 'Нужно снова войти'),
    ({'results': []}, 'failed', 'offline', 'Нет соединения'),
    ({'results': [{'target': 'alice', 'reason': 'delivery_unconfirmed'}]},
     'skipped', 'delivery_unconfirmed', 'повтор заблокирован'),
    ({'results': [{'target': 'alice', 'reason': 'send_failed'}]},
     'failed', '', '⚠️ Результат отправки требует проверки'),
    ({'success_count': 1, 'failed_count': 1, 'results': [
        {'target': 'alice', 'success': True, 'confirmation': 'server_ack'},
        {'target': 'bob', 'reason': 'chat_not_confirmed'}]},
     'partial', '', '⚠️ Отправлено не всё'),
])
def test_report_distinguishes_ack_failure_and_unknown(summary, status, reason, expected):
    assert expected in report_text(summary, status=status, reason=reason)


@pytest.mark.parametrize('manual_bot_running', [False, True])
def test_report_is_one_shot_and_never_changes_manual_bot(tmp_path, monkeypatch, manual_bot_running):
    monkeypatch.delenv('TG_DISABLE_NOTIFICATIONS', raising=False)
    from yara_app.project_adapter import ProjectAdapter
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    config = adapter.telegram_config_path
    config.write_text(json.dumps({'token': TOKEN, 'allowed_chat_ids': [123]}))
    adapter.update_control_state({'telegram_bot_pid': 999 if manual_bot_running else None})
    before = adapter.control_state_path.read_bytes()
    monkeypatch.setattr(adapter, 'start_telegram_bot', lambda: pytest.fail('controller started'))
    monkeypatch.setattr(adapter, 'stop_telegram_bot', lambda: pytest.fail('manual controller stopped'))
    calls = []
    monkeypatch.setattr(TelegramSetup, '_call', lambda self, method, payload: calls.append((method, payload)) or {'message_id': 1})
    result = notify_worker_result(adapter, {'profile_name': 'test', 'results': []}, status='failed', reason='offline')
    assert result == {'ok': True, 'sent_count': 1, 'failed_count': 0}
    assert [method for method, _ in calls] == ['sendMessage']
    assert adapter.control_state_path.read_bytes() == before


def test_report_failure_does_not_raise_or_repeat_message(tmp_path, monkeypatch):
    monkeypatch.delenv('TG_DISABLE_NOTIFICATIONS', raising=False)
    config = tmp_path / 'telegram.json'
    config.write_text(json.dumps({'token': TOKEN, 'allowed_chat_ids': [123]}))
    calls = []
    def fail(self, method, payload):
        calls.append(method)
        raise OSError('network unavailable')
    monkeypatch.setattr(TelegramSetup, '_call', fail)
    result = notify_worker_result(SimpleNamespace(telegram_config_path=config), {}, status='failed')
    assert not result['ok'] and result['failed_count'] == 1
    assert calls == ['sendMessage']


def test_test_button_sends_explicit_telegram_test_only(tmp_path, monkeypatch):
    config = tmp_path / 'telegram.json'
    config.write_text(json.dumps({'token': TOKEN, 'allowed_chat_ids': [123]}))
    setup = TelegramSetup(SimpleNamespace(telegram_config_path=config))
    calls = []
    monkeypatch.setattr(setup, '_call', lambda method, payload: calls.append((method, payload)) or {'message_id': 1})
    assert setup.test_notification() == {'delivered_count': 1}
    assert calls[0][0] == 'sendMessage'
    assert 'TikTok не запускался' in calls[0][1]['text']


@pytest.mark.parametrize('fails', [False, True])
def test_worker_report_follows_browser_close_and_lock_release(monkeypatch, tmp_path, fails):
    from contextlib import contextmanager

    import yara_app.tiktok_checker as checker

    events = []
    def screenshot(*, path, **_kwargs):
        from pathlib import Path
        Path(path).write_bytes(b'\x89PNG\r\n\x1a\nimage')
        events.append('screenshot')
    page = SimpleNamespace(screenshot=screenshot)
    monkeypatch.setattr(checker, 'PATHS', SimpleNamespace(artifacts_dir=tmp_path / 'artifacts'))
    state = SimpleNamespace(paused=False, dry_run=False, last_run_pid=None, stop_requested=False)
    @contextmanager
    def browser_runtime():
        yield object()
    monkeypatch.setattr(checker, 'run_lock', SimpleNamespace(acquire=lambda: True, release=lambda: events.append('release')))
    monkeypatch.setattr(checker.atexit, 'register', lambda *_a: None)
    monkeypatch.setattr(checker, 'control_store', SimpleNamespace(load_state=lambda: state,
        update_state=lambda **_k: None, load_profiles=lambda: {checker.ACTIVE_PROFILE: [{'name': 'alice'}]}))
    monkeypatch.setattr(checker, 'ready_targets_for_run', lambda targets, summary: targets)
    monkeypatch.setattr(checker, 'wait_for_internet', lambda: True)
    monkeypatch.setattr(checker, 'sync_playwright', browser_runtime)
    monkeypatch.setattr(checker, 'open_logged_in_work_browser', lambda _p: (True, object(), page))
    monkeypatch.setattr(checker, 'stop_or_pause_reason', lambda: None)
    monkeypatch.setattr(checker, 'safe_close_context', lambda *_a: events.append('browser_closed'))
    def process(page, target, summary):
        if fails:
            raise ValueError('Unexpected popup')
        summary.add_result(target=target.name, success=True, confirmation='server_ack')
    monkeypatch.setattr(checker, 'process_target', process)
    monkeypatch.setattr(checker, 'send_start_message', lambda *_a, **_k: None)
    monkeypatch.setattr(checker, '_write_run_state', lambda **_k: None)
    monkeypatch.setattr(checker, '_append_run_history', lambda *_a, **_k: None)
    monkeypatch.setattr(checker, 'save_run_summary', lambda _s: None)
    monkeypatch.setattr(checker, 'log_lifecycle', lambda *_a, **_k: None)
    def report(adapter, summary, **kwargs):
        if fails:
            assert summary['results'][0]['error'] == 'Unexpected popup'
            assert summary['results'][0]['screenshot_path']
        events.append('report')
    monkeypatch.setattr(checker, 'notify_worker_result', report)
    monkeypatch.setattr(checker, 'auth_coord_adapter', SimpleNamespace(scheduler=SimpleNamespace(reanchor_from_queue=lambda: {})))
    checker.check_tiktok_streak()
    assert events == (['screenshot'] if fails else []) + ['browser_closed', 'release', 'report']


def test_report_contains_actual_error_for_target_and_run_and_redacts_secrets():
    summary = {'profile_name': 'test', 'results': [
        {'target': 'alice', 'reason': 'chat_blocked', 'error': 'Всплывающее окно sessionid=private'},
        {'target': '__run__', 'reason': 'unhandled_exception', 'error': 'Exact run error ' + TOKEN}]}
    text = report_text(summary, status='failed')
    assert 'Всплывающее окно' in text and 'Exact run error' in text
    assert 'private' not in text and TOKEN not in text


@pytest.mark.parametrize('photo_fails', [False, True])
def test_error_report_delivers_text_first_then_photo_to_owner_and_admin(tmp_path, monkeypatch, photo_fails):
    monkeypatch.delenv('TG_DISABLE_NOTIFICATIONS', raising=False)
    from yara_app.project_adapter import ProjectAdapter
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.telegram_config_path.write_text(json.dumps({'token': TOKEN, 'admin_chat_ids': [100],
        'allowed_chat_ids': [100, 200, 300], 'profile_owners': {'alpha': [200]}}))
    photo = adapter._bot_profile_root('alpha') / 'artifacts' / 'error_test.png'
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b'\x89PNG\r\n\x1a\nimage')
    calls = []
    def request(self, method, payload, **kwargs):
        calls.append((method, payload, kwargs))
        if method == 'sendPhoto' and photo_fails:
            raise OSError('upload failed')
        return {'message_id': len(calls)}
    monkeypatch.setattr(TelegramSetup, '_call', request)
    result = notify_worker_result(adapter, {'profile_name': 'alpha', 'results': [
        {'target': 'alice', 'reason': 'chat_blocked', 'error': 'Всплывающее окно', 'screenshot_path': str(photo)}]}, status='failed')
    assert result['ok'] and result['sent_count'] == 2
    assert [(method, data['chat_id']) for method, data, _ in calls] == [
        ('sendMessage', 100), ('sendPhoto', 100), ('sendMessage', 200), ('sendPhoto', 200)]
    assert all('Всплывающее окно' in data.get('text', data.get('caption', '')) for _, data, _ in calls)


def test_error_attachments_reject_foreign_profile_missing_and_old_auth_images(tmp_path):
    from yara_app.notifications import error_photos
    from yara_app.project_adapter import ProjectAdapter
    adapter = ProjectAdapter(tmp_path)
    folder = adapter._bot_profile_root('alpha') / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    foreign = adapter._bot_profile_root('beta') / 'artifacts' / 'error_private.png'
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(b'private')
    auth = folder / 'auth_options.png'
    auth.write_bytes(b'old')
    assert error_photos(adapter, {'profile_name': 'alpha', 'results': [
        {'screenshot_path': str(foreign)}, {'screenshot_path': str(auth)},
        {'screenshot_path': str(folder / 'error_missing.png')}]}) == []


def test_error_photos_are_deduplicated_and_limited(tmp_path):
    from yara_app.notifications import error_photos
    from yara_app.project_adapter import ProjectAdapter
    adapter = ProjectAdapter(tmp_path)
    folder = adapter._bot_profile_root('alpha') / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(5):
        path = folder / f'error_{index}.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        entries.extend([{'screenshot_path': str(path)}] * 2)
    assert len(error_photos(adapter, {'profile_name': 'alpha', 'results': entries})) == 3


def test_photo_upload_uses_verified_https_and_multipart_without_requests(tmp_path, monkeypatch):
    photo = tmp_path / 'image.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\nimage')
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def read(self, *_args):
            return b'{"ok":true,"result":{"message_id":42}}'
    def request(req, *, timeout, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert timeout == 10
        assert req.get_header('Content-type').startswith('multipart/form-data; boundary=')
        assert b'name="photo"; filename="error.png"' in req.data
        assert photo.read_bytes() in req.data
        assert b'name="chat_id"\r\n\r\n123' in req.data
        assert 'Причина ошибки'.encode() in req.data
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', request)
    setup = TelegramSetup(SimpleNamespace(telegram_config_path=tmp_path / 'config.json'))
    assert setup._call('sendPhoto', {'chat_id': 123, 'caption': 'Причина ошибки'}, TOKEN, photo_path=photo)['message_id'] == 42

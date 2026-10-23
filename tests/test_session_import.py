from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from yara_app.project_adapter import ProjectAdapter, WorkerStatus
from yara_app.session_import import cookies_from_curl, prepare_session

REQUEST = "curl 'https://www.tiktok.com/messages' -b 'sessionid=fake-test-session; tt_csrf_token=fake-csrf'"


@pytest.mark.parametrize('text', [
    REQUEST,
    'curl "https://www.tiktok.com/messages" ^\n  -b ^"sessionid=fake-test-session; tt_csrf_token=fake-csrf^"',
    "curl 'https://www.tiktok.com/messages' \\\n -H 'Cookie: sessionid=fake-test-session; tt_csrf_token=fake-csrf'",
    REQUEST + ";\n# Unsupported URL scheme ;\ncurl --url 'https://foreign.example' -b 'sessionid=wrong'",
])
def test_chrome_curl_formats(text):
    cookies = cookies_from_curl(text)
    assert {item['name'] for item in cookies} == {'sessionid', 'tt_csrf_token'}
    assert all(item['domain'] == '.tiktok.com' and item['secure'] for item in cookies)


def test_large_copy_all_imports_only_first_request():
    text = REQUEST + ";\ncurl 'https://foreign.example' --data-raw '" + 'unused' * 100000
    cookies = cookies_from_curl(text)
    assert {item['name']: item['value'] for item in cookies} == {
        'sessionid': 'fake-test-session', 'tt_csrf_token': 'fake-csrf',
    }


def test_oversized_first_request_is_still_rejected():
    text = REQUEST + " -H 'large: " + 'unused' * 100000 + "';"
    with pytest.raises(ValueError, match='Первый запрос слишком большой'):
        cookies_from_curl(text)


@pytest.mark.parametrize('text', [
    "curl 'https://www.tiktok.com.evil.example/messages' -b 'sessionid=secret'",
    "curl 'https://evil.example/?next=https://www.tiktok.com/messages' -b 'sessionid=secret'",
    "curl 'http://www.tiktok.com/messages' -b 'sessionid=secret'",
    "curl 'https://www.tiktok.com/messages' -b 'theme=dark'",
    "curl 'https://www.tiktok.com/messages' -b 'sessionid=secret",
    'powershell secret', None, 'secret' * 50000,
], ids=['lookalike', 'foreign', 'http', 'no-auth', 'broken-quote', 'not-curl', 'null', 'oversized'])
def test_rejects_wrong_or_incomplete_request_without_leaking_it(text):
    with pytest.raises(ValueError) as error:
        cookies_from_curl(text)
    assert 'secret' not in str(error.value)


def adapter_fixture(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=False, pid=None))
    monkeypatch.setattr(adapter, 'create_auth_backup', lambda _key: {'created': True})
    old = tmp_path / 'profiles/main/browser/user_data'
    old.mkdir(parents=True)
    (old / 'old-session').write_text('keep')
    pause = tmp_path / 'profiles/main/state/auth_backoff.json'
    pause.parent.mkdir()
    pause.write_text('{}')
    (pause.parent / 'session_import_required.json').write_text('{}')
    return adapter, old, pause


def test_unverified_session_preserves_profile_and_pause(tmp_path, monkeypatch):
    adapter, old, pause = adapter_fixture(tmp_path, monkeypatch)

    def fail(stage, _cookies):
        stage.mkdir()
        raise RuntimeError('not authenticated')

    monkeypatch.setattr('yara_app.session_import.prepare_session', fail)
    with pytest.raises(RuntimeError, match='not authenticated'):
        adapter.import_chrome_request(request_text=REQUEST, bot_profile_key='main')
    assert (old / 'old-session').read_text() == 'keep'
    assert pause.exists()
    assert (pause.parent / 'session_import_required.json').exists()
    assert not list(old.parent.glob('session_import_*'))
    assert not (old.parent / '.session_import.lock').exists()


def test_verified_session_promotes_with_backup_and_clears_pause(tmp_path, monkeypatch):
    adapter, old, pause = adapter_fixture(tmp_path, monkeypatch)

    def verify(stage, _cookies):
        stage.mkdir()
        (stage / 'new-session').write_text('verified')

    monkeypatch.setattr('yara_app.session_import.prepare_session', verify)
    result = adapter.import_chrome_request(request_text=REQUEST, bot_profile_key='main')
    assert result['verified'] is True
    assert 'fake-test-session' not in str(result)
    assert (old / 'new-session').read_text() == 'verified'
    assert (Path(result['backup']) / 'old-session').read_text() == 'keep'
    assert not pause.exists()
    assert not (pause.parent / 'session_import_required.json').exists()
    assert not list(old.parent.glob('session_import_*'))


def test_busy_worker_rejects_before_browser_launch(tmp_path, monkeypatch):
    adapter, old, pause = adapter_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=True, pid=123))
    with pytest.raises(RuntimeError, match='worker'):
        adapter.import_chrome_request(request_text=REQUEST, bot_profile_key='main')
    assert (old / 'old-session').exists() and pause.exists()


def test_failed_move_does_not_remove_live_profile(tmp_path, monkeypatch):
    adapter, old, _pause = adapter_fixture(tmp_path, monkeypatch)

    def fail(*_args):
        raise PermissionError('profile in use')

    monkeypatch.setattr('yara_app.project_adapter.shutil.move', fail)
    with pytest.raises(PermissionError):
        adapter._commit_staged_browser_profile(
            browser_dir=old.parent, user_data_dir=old, staged_user_data_dir=tmp_path / 'stage', timestamp='test',
        )
    assert (old / 'old-session').read_text() == 'keep'


def test_browser_errors_are_redacted_and_context_closes(tmp_path, monkeypatch):
    closed = []

    def fail(*_args, **_kwargs):
        raise RuntimeError('sessionid=secret')

    context = SimpleNamespace(add_cookies=fail, close=lambda: closed.append(True))
    monkeypatch.setattr('yara_app.auth_flow._launch_persistent_chromium', lambda *_a, **_kw: context)
    with pytest.raises(RuntimeError) as error:
        prepare_session(tmp_path / 'stage', cookies_from_curl(REQUEST))
    assert 'secret' not in str(error.value)
    assert closed == [True]

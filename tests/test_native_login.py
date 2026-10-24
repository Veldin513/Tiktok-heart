from __future__ import annotations

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from yara_app.auth_flow import _launch_persistent_chromium
from yara_app.native_login import LOGIN_PENDING, browser_lease, mark_google_chrome, uses_google_chrome
from yara_app.project_adapter import ProjectAdapter, WorkerStatus


@pytest.fixture
def setup_login(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.update_control_state({'active_profile': 'main'})
    adapter.main_script_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.main_script_path.touch()
    active = tmp_path / 'profiles/main/browser/user_data'
    active.mkdir(parents=True)
    (active / 'old-session').write_text('preserve')
    state = active.parent.parent / 'state'
    state.mkdir()
    (state / 'session_import_required.json').write_text('{}')
    (state / 'auth_backoff.json').write_text('{}')
    calls = []
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=False, pid=None))
    monkeypatch.setattr(adapter, 'create_auth_backup', lambda key: calls.append(('backup', key)))
    monkeypatch.setattr('yara_app.project_adapter.find_google_chrome', lambda: Path('chrome.exe'))
    monkeypatch.setattr('yara_app.project_adapter.chrome_profile_running', lambda _path: False)
    monkeypatch.setattr('yara_app.project_adapter.subprocess.Popen', lambda cmd, **kw: calls.append(('launch', cmd, kw)))
    return adapter, active, state, calls


def test_manual_login_uses_native_chrome_and_preserves_old_profile(setup_login):
    adapter, active, state, calls = setup_login
    result = adapter.begin_dedicated_login('main')
    cmd = next(call[1] for call in calls if call[0] == 'launch')
    assert cmd[-1] == 'https://www.tiktok.com/messages'
    assert '--profile-directory=Default' in cmd
    assert any('dedicated_login' in arg for arg in cmd)
    assert not any('headless' in arg or 'remote-debugging' in arg or 'automation' in arg.lower() for arg in cmd)
    assert (active / 'old-session').read_text() == 'preserve'
    assert (state / 'session_import_required.json').exists()
    assert result['pending'] and (active.parent / LOGIN_PENDING).exists()
    with pytest.raises(RuntimeError, match='ручной вход'):
        adapter.start_worker()


def test_failed_verification_or_open_chrome_never_replaces_profile(setup_login, monkeypatch):
    adapter, active, state, _calls = setup_login
    adapter.begin_dedicated_login('main')
    checked = []
    monkeypatch.setattr('yara_app.project_adapter.verify_chrome_session', lambda path: checked.append(path))
    monkeypatch.setattr('yara_app.project_adapter.chrome_profile_running', lambda _path: True)
    with pytest.raises(RuntimeError, match='Закройте'):
        adapter.finish_dedicated_login('main')
    assert not checked
    monkeypatch.setattr('yara_app.project_adapter.chrome_profile_running', lambda _path: False)

    def fail(_path):
        raise RuntimeError('not verified')

    monkeypatch.setattr('yara_app.project_adapter.verify_chrome_session', fail)
    with pytest.raises(RuntimeError, match='not verified'):
        adapter.finish_dedicated_login('main')
    assert (active / 'old-session').exists() and (state / 'auth_backoff.json').exists()
    assert (active.parent / LOGIN_PENDING).exists()


def test_verified_login_promotes_then_reauthentication_reuses_profile(setup_login, monkeypatch):
    adapter, active, state, calls = setup_login
    adapter.begin_dedicated_login('main')
    monkeypatch.setattr('yara_app.project_adapter.verify_chrome_session', lambda _path: None)
    result = adapter.finish_dedicated_login('main')
    assert result['verified'] and uses_google_chrome(active)
    assert (Path(result['backup']) / 'old-session').exists()
    assert not (state / 'session_import_required.json').exists()
    assert not (state / 'auth_backoff.json').exists()
    assert not (active.parent / LOGIN_PENDING).exists()
    validation = json.loads((state / 'session_validation.json').read_text())
    assert validation['valid'] is True and validation['source'] == 'dedicated_chrome'
    calls.clear()
    (active / 'device-storage').write_text('same-device')
    adapter.begin_dedicated_login('main')
    cmd = next(call[1] for call in calls if call[0] == 'launch')
    assert f'--user-data-dir={active}' in cmd
    assert (active / 'device-storage').read_text() == 'same-device'
    adapter.cancel_dedicated_login('main')
    assert (state / 'session_import_required.json').exists()


def test_cancel_staged_login_preserves_previous_guards_and_no_relaunch(setup_login, monkeypatch):
    adapter, active, state, calls = setup_login
    monkeypatch.setattr('yara_app.project_adapter.chrome_profile_running', lambda _path: True)
    assert adapter.begin_dedicated_login('main')['already_open']
    assert not calls
    monkeypatch.setattr('yara_app.project_adapter.chrome_profile_running', lambda _path: False)
    adapter.cancel_dedicated_login('main')
    assert not (active.parent / LOGIN_PENDING).exists()
    assert (active / 'old-session').exists() and (state / 'session_import_required.json').exists()


def test_lease_blocks_parallel_browser_mutations_and_is_released(setup_login):
    adapter, active, _state, _calls = setup_login
    with browser_lease(active.parent):
        for operation in (lambda: adapter.begin_dedicated_login('main'),
                          lambda: adapter.compact_browser_profile('main')):
            with pytest.raises(RuntimeError, match='занят'):
                operation()
    with browser_lease(active.parent):
        pass


def test_browser_lease_is_released_by_os_after_process_crash(tmp_path):
    code = (
        'import sys; from pathlib import Path; from yara_app.native_login import acquire_browser_lease; '
        'lease=acquire_browser_lease(Path(sys.argv[1])); print("ready",flush=True); sys.stdin.readline()'
    )
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(RuntimeError, match='занят'), browser_lease(tmp_path):
            pass
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
    with browser_lease(tmp_path):
        pass


def test_busy_worker_prevents_manual_login(setup_login, monkeypatch):
    adapter, active, _state, calls = setup_login
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=True, pid=123))
    with pytest.raises(RuntimeError, match='worker'):
        adapter.begin_dedicated_login('main')
    assert not calls and not (active.parent / LOGIN_PENDING).exists()


def test_verification_errors_do_not_expose_session_data(tmp_path, monkeypatch):
    from yara_app.native_login import verify_chrome_session

    closed = []

    def fail(*_args, **_kwargs):
        raise RuntimeError('sessionid=secret-test-value')

    context = SimpleNamespace(pages=[SimpleNamespace(goto=fail)], close=lambda: closed.append(True))
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: nullcontext(object()))
    monkeypatch.setattr('yara_app.auth_flow._launch_persistent_chromium', lambda *_a, **_kw: context)
    with pytest.raises(RuntimeError) as error:
        verify_chrome_session(tmp_path)
    assert 'secret-test-value' not in str(error.value) and closed == [True]


def test_launch_uses_profile_browser_and_releases_lease_on_close_and_error(tmp_path, monkeypatch):
    data = tmp_path / 'browser/user_data'
    mark_google_chrome(data)
    callbacks = []
    context = SimpleNamespace(on=lambda _event, fn: callbacks.append(fn))
    options = []
    playwright = SimpleNamespace(chromium=SimpleNamespace(
        launch_persistent_context=lambda **kw: options.append(kw) or context,
    ))
    monkeypatch.setattr('yara_app.auth_flow.chrome_profile_running', lambda _path: False)
    monkeypatch.setattr('yara_app.auth_flow.TIKTOK_BROWSER_CHANNEL', 'msedge')
    _launch_persistent_chromium(playwright, data, headless=True)
    assert options[0]['channel'] == 'chrome' and options[0]['headless']
    assert (data.parent / '.browser_session.lock').exists()
    with pytest.raises(RuntimeError, match='занят'):
        _launch_persistent_chromium(playwright, data, headless=True)
    callbacks[0]()
    with browser_lease(data.parent):
        pass
    (data.parent / LOGIN_PENDING).write_text('{}')
    with pytest.raises(RuntimeError, match='Ручной вход'):
        _launch_persistent_chromium(playwright, data, headless=True)
    with browser_lease(data.parent):
        pass


def test_chrome_process_detection_matches_exact_profile(tmp_path, monkeypatch):
    from yara_app.native_login import chrome_profile_running

    other = tmp_path / 'other'
    target = tmp_path / 'target'
    proc = SimpleNamespace(info={'name': 'chrome.exe', 'cmdline': ['chrome', f'--user-data-dir={other}']})
    monkeypatch.setattr('psutil.process_iter', lambda *_args: [proc])
    assert not chrome_profile_running(target)
    proc.info['cmdline'] = ['chrome', '--user-data-dir', str(target)]
    assert chrome_profile_running(target)


def test_dedicated_profile_compaction_preserves_site_state(setup_login):
    adapter, active, _state, _calls = setup_login
    mark_google_chrome(active)
    for rel in ('Default/Service Worker/db', 'Default/IndexedDB/https_other_0/db',
                'Default/Local Storage/db', 'Default/Network/Cookies', 'Default/Cache/db'):
        path = active / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic-persistent-state')
    before = (active / 'Default/Network/Cookies').read_bytes()
    adapter.compact_browser_profile('main')
    assert not (active / 'Default/Cache').exists()
    assert (active / 'Default/Service Worker/db').exists()
    assert (active / 'Default/IndexedDB/https_other_0/db').exists()
    assert (active / 'Default/Local Storage/db').exists()
    assert (active / 'Default/Network/Cookies').read_bytes() == before


def test_installed_chrome_persists_synthetic_cookie_after_promotion(tmp_path):
    from playwright.sync_api import sync_playwright

    from yara_app.native_login import find_google_chrome

    try:
        find_google_chrome()
    except RuntimeError:
        pytest.skip('Installed Google Chrome not available')
    stage = tmp_path / 'browser/dedicated_login/user_data'
    mark_google_chrome(stage)
    with sync_playwright() as playwright:
        context = _launch_persistent_chromium(playwright, stage, headless=True)
        context.add_cookies([{'name': 'synthetic', 'value': 'local-test', 'domain': 'example.test',
                             'path': '/', 'expires': 2000000000}])
        context.close()
        adapter = ProjectAdapter(tmp_path)
        active = tmp_path / 'browser/user_data'
        adapter._commit_staged_browser_profile(
            browser_dir=active.parent, user_data_dir=active, staged_user_data_dir=stage, timestamp='test',
        )
        context = _launch_persistent_chromium(playwright, active, headless=True)
        try:
            assert uses_google_chrome(active)
            assert any(cookie['name'] == 'synthetic' and cookie['value'] == 'local-test' for cookie in context.cookies())
        finally:
            context.close()

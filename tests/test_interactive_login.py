from __future__ import annotations

import json
import struct
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from yara_app.interactive_login import InteractiveLogin, LoginPage
from yara_app.local_login import LocalLogin
from yara_app.native_login import LOGIN_PENDING, mark_google_chrome
from yara_app.project_adapter import ProjectAdapter


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Login did not reach the expected step')


@pytest.fixture
def login(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.create_profile('alpha')
    active = adapter._bot_profile_root('alpha') / 'browser' / 'user_data'
    active.mkdir(parents=True)
    (active / 'old-session').write_text('keep')
    events = []
    received = []
    permitted = [True]
    pages = []
    class Page:
        logged = False
        submitted = False
        def goto(self, *_a, **_kw):
            pass
        def set_default_timeout(self, _value):
            pass
    class Context:
        def __init__(self):
            self.pages = [Page()]
            pages.extend(self.pages)
        def close(self):
            events.append(('closed', None, None))
    class View:
        fail = False
        def __init__(self, page):
            self.page = page
        def open(self, method):
            received.append(('method', method))
        def qr_photo(self, path):
            path.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        def password(self, identifier, password):
            received.append(('password', identifier, password))
            if View.fail:
                raise RuntimeError('Call log: fill(' + password + ')')
            self.page.submitted = True
        def code(self, code):
            received.append(('code', code))
            self.page.logged = True
        def authenticated(self):
            return self.page.logged
        def code_fields(self):
            return object() if self.page.submitted else None
        def code_request_available(self):
            return False
        def challenge(self):
            return False
        def rejected(self):
            return False
        def confirmation_photo(self, path):
            return None
    @contextmanager
    def runtime():
        yield object()
    def launch(_runtime, target, **kwargs):
        assert kwargs == {'headless': True, '_acquire_lease': False}
        cookie = target / 'Default' / 'Network' / 'Cookies'
        cookie.parent.mkdir(parents=True, exist_ok=True)
        cookie.write_bytes(b'fake-cookie')
        return Context()
    monkeypatch.setattr('playwright.sync_api.sync_playwright', runtime)
    monkeypatch.setattr('yara_app.auth_flow._launch_persistent_chromium', launch)
    monkeypatch.setattr('yara_app.auth_flow.wait_for_authenticated_page', lambda page, **_kw: page.logged)
    monkeypatch.setattr('yara_app.interactive_login.chrome_profile_running', lambda _p: False)
    monkeypatch.setattr('yara_app.interactive_login.LoginPage', View)
    monkeypatch.setattr(adapter, 'start_profile_worker', lambda *_a, **_kw: pytest.fail('login started sending'))
    manager = InteractiveLogin(adapter, lambda session, text, photo: events.append((session.stage, text, photo)),
        lambda owner, profile: permitted[0] and owner == 200 and profile == 'alpha')
    yield SimpleNamespace(adapter=adapter, manager=manager, events=events, received=received,
                          active=active, pages=pages, permitted=permitted, view=View)
    manager.close()


def test_password_and_confirmation_are_sent_only_to_own_session_and_not_saved(login):
    manager = login.manager
    session = manager.start('alpha', 200, 'password')
    wait_until(lambda: session.stage == 'identifier')
    assert manager.submit(300, 'foreign secret') is False
    with pytest.raises(PermissionError):
        manager.command(300, session.id, 'cancel')
    manager.submit(200, 'demo_user')
    wait_until(lambda: session.stage == 'password')
    manager.submit(200, ' secret with spaces ')
    wait_until(lambda: session.stage == 'code')
    with pytest.raises(ValueError):
        manager.submit(200, 'not-code')
    manager.submit(200, '123456')
    session.thread.join(timeout=5)
    assert not session.thread.is_alive()
    assert ('password', 'demo_user', ' secret with spaces ') in login.received
    assert ('code', '123456') in login.received
    assert any(item[0] == 'done' for item in login.events)
    assert next(i for i, e in enumerate(login.events) if e[0] == 'closed') < next(i for i, e in enumerate(login.events) if e[0] == 'done')
    root = login.adapter._bot_profile_root('alpha')
    assert json.loads((root / 'state/session_validation.json').read_text())['valid'] is True
    assert not (root / 'browser' / LOGIN_PENDING).exists()
    assert session.inbox.empty() and manager.current(200) is None
    for file in login.adapter.base_dir.rglob('*'):
        if file.is_file() and file.suffix in {'.json', '.log', '.txt'}:
            assert 'secret with spaces' not in file.read_text(errors='replace')


def test_qr_cancel_preserves_original_profile_and_removes_temporary_photo(login):
    session = login.manager.start('alpha', 200, 'qr')
    wait_until(lambda: session.stage == 'qr')
    photo = next(e[2] for e in login.events if e[0] == 'qr')
    assert photo.is_file()
    with pytest.raises(ValueError):
        login.manager.start('alpha', 200, 'password')
    login.manager.cancel(200)
    assert (login.active / 'old-session').read_text() == 'keep'
    assert not photo.exists()
    assert not (login.active.parent / LOGIN_PENDING).exists()
    assert not login.manager.sessions


def test_qr_success_is_checked_before_session_is_saved(login):
    session = login.manager.start('alpha', 200, 'qr')
    wait_until(lambda: session.stage == 'qr')
    assert not (login.active.parent / LOGIN_PENDING).is_dir()
    login.pages[0].logged = True
    login.manager.command(200, session.id, 'check')
    session.thread.join(timeout=5)
    assert any(e[0] == 'done' for e in login.events)
    assert not (login.active / 'old-session').exists()  # Backed up before the verified promotion.
    assert any(p.name == 'old-session' for p in login.active.parent.rglob('old-session'))


def test_password_in_playwright_exception_is_never_logged_or_sent(login, caplog):
    login.view.fail = True
    session = login.manager.start('alpha', 200, 'password')
    wait_until(lambda: session.stage == 'identifier')
    login.manager.submit(200, 'test')
    wait_until(lambda: session.stage == 'password')
    login.manager.submit(200, 'VERY_PRIVATE_PASSWORD')
    session.thread.join(timeout=5)
    assert 'VERY_PRIVATE_PASSWORD' not in caplog.text
    assert 'VERY_PRIVATE_PASSWORD' not in str(login.events)
    assert (login.active / 'old-session').read_text() == 'keep'


def test_revoking_access_stops_login_without_saving_new_session(login):
    session = login.manager.start('alpha', 200, 'qr')
    wait_until(lambda: session.stage == 'qr')
    login.permitted[0] = False
    session.thread.join(timeout=3)
    assert not session.thread.is_alive()
    assert (login.active / 'old-session').exists()
    assert not (login.active.parent / LOGIN_PENDING).exists()


def test_worker_and_open_native_window_block_login(login, monkeypatch):
    monkeypatch.setattr(login.adapter, 'get_worker_status', lambda: SimpleNamespace(running=True))
    with pytest.raises(ValueError):
        login.manager.start('alpha', 200, 'qr')
    monkeypatch.setattr(login.adapter, 'get_worker_status', lambda: SimpleNamespace(running=False))
    monkeypatch.setattr('yara_app.interactive_login.chrome_profile_running', lambda _p: True)
    session = login.manager.start('alpha', 200, 'qr')
    session.thread.join(timeout=3)
    assert not (login.active.parent / LOGIN_PENDING).exists()
    assert any(e[0] == 'failed' for e in login.events)


def test_expiration_and_cancel_of_existing_chrome_profile(login):
    mark_google_chrome(login.active)
    manager = InteractiveLogin(login.adapter, lambda *_a: None, lambda *_a: True, timeout_seconds=0.01)
    session = manager.start('alpha', 200, 'qr')
    session.thread.join(timeout=3)
    assert not session.thread.is_alive()
    assert (login.active / 'old-session').exists()
    state = json.loads((login.active.parent.parent / 'state/session_validation.json').read_text())
    assert state['valid'] is None
    assert not (login.active.parent / LOGIN_PENDING).exists()


def test_local_status_never_returns_password_or_identifier(login):
    service = LocalLogin(login.adapter)
    session = SimpleNamespace(id='id', profile='alpha', stage='password')
    service._event(session, 'internal message')
    status = service.status('id')
    assert status['message'] == 'Введите пароль.'
    assert status['field'] == 'password'
    assert set(status) == {'id', 'profile', 'stage', 'message', 'revision', 'has_photo', 'field'}
    with pytest.raises(ValueError):
        service.status('wrong-id')


def test_fast_cancel_cannot_overwrite_finished_state_with_cancelling(login):
    service = LocalLogin(login.adapter)
    session = SimpleNamespace(id='id', profile='alpha', stage='identifier')
    service._event(session, 'Enter name')
    def command(*_a):
        session.stage = 'cancelled'
        service._event(session, 'Вход отменён.')
    service.engine = SimpleNamespace(command=command, current=lambda _owner: None)
    assert service.command('id', 'cancel')['stage'] == 'cancelled'


def test_actual_ui_selectors_pick_qr_not_title_and_accept_password_and_code(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<h1 data-e2e="qr-title">Log in with QR code</h1>
                <div data-e2e="qr-code"><canvas width="170" height="170"></canvas></div>
                <input name="username"><input type="password"><button>Log in</button>
                <input autocomplete="one-time-code"><button>Verify</button>''')
            view = LoginPage(page)
            image = tmp_path / 'qr.png'
            view.qr_photo(image)
            width, height = struct.unpack('>II', image.read_bytes()[16:24])
            assert width == 170 and 170 <= height <= 171
            view.password('user', 'test password')
            assert page.locator('input[type=password]').input_value() == 'test password'
            view.code('123456')
            assert page.locator('input[autocomplete="one-time-code"]').input_value() == '123456'
        finally:
            browser.close()

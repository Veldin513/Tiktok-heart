from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from app_shell.server import AppShellHandler
from yara_app.project_adapter import ProjectAdapter


@pytest.fixture
def login_server(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.create_profile('alpha')
    class Login:
        def __init__(self):
            self.adapter = adapter
            self.values = []
            self.data = {}
        def start(self, profile, method):
            self.data = {'id': 'test-session', 'profile': profile, 'stage': 'identifier',
                         'field': 'identifier', 'message': 'Введите логин или почту TikTok.', 'has_photo': False}
            return self.data
        def status(self, sid):
            if sid != self.data.get('id'):
                raise ValueError('Вход уже закрыт.')
            return self.data
        def submit(self, sid, value):
            self.status(sid)
            self.values.append(value)
            field = ['password', 'code', None][len(self.values) - 1]
            self.data.update(field=field, stage=field or 'done',
                             message={'password': 'Введите пароль.', 'code': 'Введите код подтверждения.', None: 'Вы вошли.'}[field])
            return self.data
        def command(self, sid, action):
            self.status(sid)
            self.data.update(field=None, stage='cancelled', message='Вход отменён.')
            return self.data
        def close(self):
            pass
    service = Login()
    monkeypatch.setattr(AppShellHandler, 'adapter', adapter)
    monkeypatch.setattr(AppShellHandler, '_login_service', service)
    AppShellHandler._clear_diagnostics_cache()
    server = ThreadingHTTPServer(('127.0.0.1', 0), AppShellHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], service
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    AppShellHandler._clear_diagnostics_cache()


def test_desktop_login_api_never_echoes_password_or_code_and_rejects_cross_origin(login_server):
    port, service = login_server
    connection = http.client.HTTPConnection('127.0.0.1', port)
    def post(path, payload, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        connection.request('POST', path, json.dumps(payload), headers)
        response = connection.getresponse()
        body = response.read().decode()
        return response.status, body
    status, body = post('/api/login/start', {'profile': 'alpha', 'method': 'password'})
    assert status == 200
    for value in ['demo_user', 'PRIVATE_DUMMY_PASSWORD', '123456']:
        status, body = post('/api/login/input', {'id': 'test-session', 'value': value})
        assert status == 200 and value not in body
    previous = list(service.values)
    status, _body = post('/api/login/input', {'id': 'test-session', 'value': 'BAD'}, 'https://foreign.example')
    assert status == 403 and service.values == previous
    connection.close()


def test_queue_panel_shows_order_and_cancel_does_not_launch_worker(login_server, monkeypatch):
    from playwright.sync_api import sync_playwright

    from yara_app.worker_queue import WorkerQueue
    port, _service = login_server
    adapter = AppShellHandler.adapter
    adapter.create_profile('beta')
    adapter.update_control_state({'paused': True})
    monkeypatch.setattr(WorkerQueue, 'kick', lambda _self: pytest.fail('viewing/cancelling started queue'))
    for key in ['alpha', 'beta']:
        profile = next(p for p in adapter.get_profiles() if p.key == key)
        adapter.worker_queue().enqueue_locked(profile)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{port}/')
            page.locator('#mode-toggle').click()
            page.locator('[data-view="services"]').click()
            box = page.locator('#worker-queue-list')
            box.get_by_text('1. alpha', exact=True).wait_for()
            assert box.get_by_text('2. beta', exact=True).is_visible()
            box.locator('.worker-queue-row').first.get_by_role('button', name='Отменить', exact=True).click()
            box.get_by_text('1. beta', exact=True).wait_for()
            assert [j['profile'] for j in adapter.worker_queue().read()['waiting']] == ['beta']
            page.locator('#worker-queue-clear').click()
            box.get_by_text('Ожидающих запусков нет.', exact=True).wait_for()
            assert not adapter.worker_queue().read()['waiting']
            assert not errors
        finally:
            browser.close()


def test_desktop_login_form_walks_username_password_code_and_clears_input(login_server):
    from playwright.sync_api import sync_playwright
    port, service = login_server
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{port}/')
            page.locator('[data-view="browser"]').click()
            page.locator('#bot-profile').select_option('alpha')
            page.locator('[data-login-method="password"]').click()
            page.locator('#simple-login-form:not([hidden])').wait_for()
            assert page.locator('#simple-login-label').inner_text() == 'Логин или почта'
            page.locator('#simple-login-value').fill('demo_user')
            page.locator('#simple-login-form button').click()
            page.wait_for_function('() => document.querySelector("#simple-login-value").type === "password"')
            assert page.locator('#simple-login-value').input_value() == ''
            assert page.locator('#bot-profile').is_disabled()
            page.locator('#simple-login-value').fill('PRIVATE_DUMMY_PASSWORD')
            page.locator('#simple-login-form button').click()
            page.wait_for_function('() => document.querySelector("#simple-login-label").textContent === "Код подтверждения"')
            assert page.locator('#simple-login-value').input_value() == ''
            assert 'PRIVATE_DUMMY_PASSWORD' not in page.content()
            page.locator('#simple-login-value').fill('123456')
            page.locator('#simple-login-form button').click()
            page.locator('#simple-login-status').get_by_text('Вы вошли.', exact=True).wait_for()
            assert page.locator('#simple-login-form').is_hidden()
            assert page.locator('#bot-profile').is_enabled()
            assert service.values == ['demo_user', 'PRIVATE_DUMMY_PASSWORD', '123456']
            assert not errors
        finally:
            browser.close()

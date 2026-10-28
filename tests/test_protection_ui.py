from __future__ import annotations

import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app_shell.server import AppShellHandler
from yara_app.project_adapter import ProjectAdapter, WorkerStatus


@pytest.fixture
def protection_server(tmp_path, monkeypatch):
    monkeypatch.setenv('HEART_KEYS_DIR', str(tmp_path / 'private_keys'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    adapter = ProjectAdapter(tmp_path / 'app')
    adapter.ensure_runtime_files()
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(False, None))
    monkeypatch.setattr(adapter, 'get_telegram_bot_status', lambda: WorkerStatus(False, None))
    monkeypatch.setattr(AppShellHandler, 'adapter', adapter)
    AppShellHandler._clear_diagnostics_cache()
    server = ThreadingHTTPServer(('127.0.0.1', 0), AppShellHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, adapter
    server.shutdown()
    server.server_close()
    thread.join(3)
    AppShellHandler._clear_diagnostics_cache()


def request(port, path, payload=None, origin=None):
    client = http.client.HTTPConnection('127.0.0.1', port)
    headers = {'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    client.request('GET' if payload is None else 'POST', path, None if payload is None else json.dumps(payload), headers)
    response = client.getresponse()
    status, text, cache = response.status, response.read().decode(), response.getheader('Cache-Control')
    client.close()
    return status, text, cache


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_recovery_phrase_requires_explicit_local_post_and_is_never_in_status(protection_server):
    port, _adapter = protection_server
    assert request(port, '/api/protection/enable', {})[0] == 200
    status, text, cache = request(port, '/api/protection/recovery', {})
    assert status == 200 and cache == 'no-store'
    phrase = json.loads(text)['data']['phrase']
    assert len(''.join(phrase.split())) == 48
    assert request(port, '/api/protection/recovery')[0] != 200
    assert request(port, '/api/protection/recovery', {}, 'https://foreign.example')[0] == 403
    for route in ['/api/protection', '/api/live', '/api/diagnostics']:
        status, text, _cache = request(port, route)
        assert status == 200 and phrase not in text
        assert '"phrase"' not in text


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_protection_screen_never_persists_phrase_in_browser_storage(protection_server):
    from playwright.sync_api import sync_playwright
    port, _adapter = protection_server
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 850})
        errors, urls = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda req: urls.append(req.url))
        try:
            page.goto(f'http://127.0.0.1:{port}/')
            page.locator('[data-view="protection"]').click()
            page.locator('#protection-enable').click()
            page.locator('#protection-recovery-result:not([hidden])').wait_for()
            phrase = page.locator('#protection-phrase').input_value()
            assert len(''.join(phrase.split())) == 48
            assert phrase not in page.evaluate('JSON.stringify(localStorage) + JSON.stringify(sessionStorage)')
            assert all(phrase not in url for url in urls)
            page.locator('[data-view="profiles"]').click()
            assert page.locator('#protection-phrase').input_value() == ''
            assert page.locator('#protection-recovery-result').is_hidden()
            assert not errors
        finally:
            browser.close()

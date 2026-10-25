from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from app_shell.server import AppShellHandler, ThreadingHTTPServer
from yara_app.fileio import atomic_write_text, file_lock, read_json
from yara_app.paths import profile_directory
from yara_app.processes import run_bounded
from yara_app.project_adapter import ProjectAdapter
from yara_app.redaction import redact_value
from yara_app.telegram_control_bot import TelegramControlBot
from yara_app.telegram_setup import TelegramSetup
from yara_app.ttbot import models

TOKEN = '123456789:' + 'test_secret_' * 4


def test_state_updates_preserve_bot_fields_and_concurrent_ui_changes(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    monkeypatch.setattr(models, 'CONTROL_STATE_FILE', adapter.control_state_path)
    store = object.__new__(models.ControlStore)
    adapter.update_control_state({'telegram_bot_pid': 8765, 'custom_setting': 'keep'})
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(adapter.update_control_state, {f'ui_{i}': i}) for i in range(20)]
        futures += [executor.submit(store.update_state, paused=True) for _ in range(20)]
        for future in futures:
            future.result()
    data = read_json(adapter.control_state_path)
    assert data['telegram_bot_pid'] == 8765 and data['custom_setting'] == 'keep'
    assert all(data[f'ui_{i}'] == i for i in range(20))


def test_atomic_writer_with_concurrent_windows_readers(tmp_path):
    path = tmp_path / 'state.json'
    atomic_write_text(path, '{"i":0}')
    def reader():
        for _ in range(200):
            assert isinstance(read_json(path)['i'], int)
    def writer():
        for i in range(100):
            atomic_write_text(path, json.dumps({'i': i}))
    with ThreadPoolExecutor(max_workers=5) as executor:
        for future in [executor.submit(reader) for _ in range(4)] + [executor.submit(writer)]:
            future.result()


def test_process_lock_cannot_be_stolen_and_is_released_on_crash(tmp_path):
    path = tmp_path / 'state.json'
    ready = tmp_path / 'ready'
    code = ('import sys,time; from pathlib import Path; from yara_app.fileio import file_lock; '
            'lock=file_lock(Path(sys.argv[1])); lock.__enter__(); Path(sys.argv[2]).touch(); time.sleep(30)')
    child = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        with pytest.raises(TimeoutError), file_lock(path, timeout=.15):
            pytest.fail('live lock was stolen')
    finally:
        child.kill()
        child.wait(timeout=5)
    with file_lock(path, timeout=1):
        pass


@pytest.mark.parametrize('name', ['profiles.json', 'control_state.json'])
def test_corrupted_settings_are_never_reset(tmp_path, name):
    path = tmp_path / 'control' / name
    path.parent.mkdir()
    path.write_text('{broken')
    with pytest.raises(ValueError):
        ProjectAdapter(tmp_path).ensure_runtime_files()
    assert path.read_text() == '{broken'


@pytest.mark.parametrize('key', ['..', '.', '../escape', 'x:y', 'CON', 'x\\y', 'x.', ' x'])
def test_all_profile_entry_points_reject_unsafe_directory_names(tmp_path, key):
    with pytest.raises(ValueError):
        profile_directory(tmp_path, key)


def test_timeout_kills_descendant_with_inherited_output(tmp_path):
    pid_file = tmp_path / 'child.pid'
    script = ("import subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "Path(sys.argv[1]).write_text(str(child.pid)); print('started',flush=True); time.sleep(30)")
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, '-c', script, str(pid_file)], timeout=1,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert time.monotonic() - start < 5
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    try:
        child = psutil.Process(pid)
        child.wait(timeout=2)
    except psutil.NoSuchProcess:
        pass


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    monkeypatch.setattr(AppShellHandler, 'adapter', ProjectAdapter(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), AppShellHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize(('headers', 'expected'), [
    ({'Host': 'evil.test'}, 403), ({'Origin': 'https://evil.test'}, 403),
    ({'Origin': 'null'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
    ({'Content-Type': 'text/plain'}, 415), ({}, 200),
])
def test_local_api_guards_execute_before_dispatch(local_server, headers, expected):
    connection = http.client.HTTPConnection('127.0.0.1', local_server, timeout=3)
    try:
        connection.request('POST', '/api/telegram/check', '{}',
                           {'Content-Type': 'application/json', **headers})
        response = connection.getresponse()
        # Valid request reaches the handler but fails because no token exists.
        assert response.status == (400 if expected == 200 else expected)
        response.read()
    finally:
        connection.close()


def test_telegram_settings_keep_secret_and_preserve_unknown_options(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    monkeypatch.setattr(adapter, 'get_telegram_bot_status', lambda: SimpleNamespace(running=False))
    setup = TelegramSetup(adapter)
    adapter.control_dir.mkdir(exist_ok=True)
    atomic_write_text(setup.path, json.dumps({'token': TOKEN, 'poll_timeout': 41, 'extra': 'keep'}))
    setup.save({'allowed_chat_ids': '123, 456', 'admin_name': 'Owner'})
    assert TOKEN not in json.dumps(setup.public_settings())
    saved = read_json(setup.path)
    assert saved['token'] == TOKEN and saved['extra'] == 'keep' and saved['poll_timeout'] == 41
    with pytest.raises(ValueError):
        setup.save({'allowed_chat_ids': []})
    monkeypatch.setattr(adapter, 'get_telegram_bot_status', lambda: SimpleNamespace(running=True))
    with pytest.raises(ValueError):
        setup.save({'allowed_chat_ids': [789]})


def test_telegram_preview_never_auto_authorizes_or_executes_messages(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    monkeypatch.setattr(adapter, 'get_telegram_bot_status', lambda: SimpleNamespace(running=False))
    setup = TelegramSetup(adapter)
    monkeypatch.setattr(setup, '_call', lambda method, payload, token: [
        {'message': {'chat': {'type': 'private', 'id': 123, 'first_name': '<b>Owner</b>'}, 'text': '/start'}},
        {'message': {'chat': {'type': 'private', 'id': 456}, 'text': '/start_worker'}},
        {'message': {'chat': {'type': 'group', 'id': -789}, 'text': '/start'}},
    ])
    assert setup.discover_chats(TOKEN) == [{'id': 123, 'name': '<b>Owner</b>', 'username': ''}]
    assert not setup.path.exists()


def test_telegram_fails_closed_without_owner_and_for_removed_owner(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    adapter.control_dir.mkdir(exist_ok=True)
    atomic_write_text(adapter.telegram_config_path, json.dumps({'token': TOKEN, 'allowed_chat_ids': []}))
    with pytest.raises(ValueError, match='allowed_chat_ids'):
        TelegramControlBot(tmp_path)
    bot = object.__new__(TelegramControlBot)
    bot.settings = SimpleNamespace(allowed_chat_ids=set())
    assert not bot._authorized(123)
    bot.settings.allowed_chat_ids = {456}
    assert not bot._authorized(123) and bot._authorized(456)


def test_telegram_corrupted_offset_is_not_reset_and_old_token_offset_is_not_reused(tmp_path):
    import hashlib
    bot = object.__new__(TelegramControlBot)
    bot.settings = SimpleNamespace(token=TOKEN)
    bot.state_path = tmp_path / 'telegram_state.json'
    atomic_write_text(bot.state_path, json.dumps({'offset': 'broken'}))
    with pytest.raises(ValueError):
        bot._load_state()
    atomic_write_text(bot.state_path, json.dumps({'offset': 555, 'known_chat_ids': [123],
                                                 'token_fingerprint': hashlib.sha256(b'old token').hexdigest()}))
    assert bot._load_state() == (None, set())


@pytest.mark.parametrize('member', ['profiles/main/state/..\\..\\evil.py',
                                  'profiles/main/state/file:stream', '../escape'])
def test_backup_rejects_windows_traversal_and_ads(tmp_path, member):
    assert ProjectAdapter(tmp_path)._safe_zip_name(member) is False


def test_restore_rolls_back_already_replaced_directories(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    browser = tmp_path / 'profiles/main/browser/user_data/Local State'
    state = tmp_path / 'profiles/main/state/example.txt'
    for path in (browser, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('backup')
    backup = adapter.create_auth_backup('main')
    browser.write_text('current-browser')
    state.write_text('current-state')
    rename = Path.rename
    def fail_once(path, target):
        if path.name == 'state' and 'new' in path.parts:
            raise PermissionError('simulated disk error')
        return rename(path, target)
    monkeypatch.setattr(Path, 'rename', fail_once)
    with pytest.raises(RuntimeError):
        adapter.restore_auth_backup('main', backup['name'])
    assert browser.read_text() == 'current-browser' and state.read_text() == 'current-state'


def test_oversized_zip_is_rejected_before_decompression(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    folder = tmp_path / 'backups/auth'
    folder.mkdir(parents=True)
    path = folder / 'auth_backup_main_test.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('profiles/main/state/a', 'a')
    original = zipfile.ZipFile.infolist
    def oversized(archive):
        infos = original(archive)
        infos[0].file_size = 5 * 1024**3
        return infos
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', oversized)
    monkeypatch.setattr(zipfile.ZipFile, 'testzip', lambda _: pytest.fail('must not decompress'))
    assert not adapter.validate_auth_backup('main', path.name, deep=True)['ok']


def test_secrets_redacted_in_nested_diagnostics():
    data = redact_value({'error': [f'https://api.telegram.org/bot{TOKEN}/getMe sessionid=secret; access_key=secret']})
    assert TOKEN not in str(data) and 'secret' not in str(data)


def test_telegram_form_save_and_edit_in_hidden_browser(local_server, monkeypatch):
    from playwright.sync_api import sync_playwright
    browsers = Path(__file__).resolve().parents[1] / '.playwright-browsers'
    if not browsers.exists():
        pytest.skip('Install project-local Playwright Chromium for UI smoke test')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(browsers))
    monkeypatch.setattr(TelegramSetup, '_call', lambda self, method, payload, token: (
        {'first_name': 'Test bot', 'username': 'test_bot'} if method == 'getMe'
        else [{'message': {'chat': {'type': 'private', 'id': 12345, 'first_name': 'Owner'}, 'text': '/start'}}]
    ))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 820})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{local_server}/')
            page.locator('#mode-toggle').click()
            page.locator('[data-view="services"]').click()
            page.wait_for_function('window.document.querySelector("#telegram-admin").value === "Admin"')
            page.locator('#telegram-token').fill(TOKEN)
            page.locator('#telegram-check').click()
            page.get_by_text('Токен принят:', exact=False).wait_for()
            page.locator('#telegram-discover').click()
            page.locator('#telegram-chat-choices button').click()
            page.locator('#telegram-admin').fill('New owner')
            page.locator('#telegram-settings-form button[type=submit]').click()
            page.get_by_text('Сохранено. Нажмите', exact=False).wait_for()
            assert page.locator('#telegram-token').input_value() == ''
            response = page.request.get(f'http://127.0.0.1:{local_server}/api/telegram/settings')
            assert TOKEN not in response.text()
            assert response.json()['data']['allowed_chat_ids'] == [12345]
            page.locator('#telegram-admin').fill('Edited owner')
            page.locator('#telegram-settings-form button[type=submit]').click()
            page.get_by_text('Сохранено. Нажмите', exact=False).wait_for()
            assert not errors
        finally:
            browser.close()

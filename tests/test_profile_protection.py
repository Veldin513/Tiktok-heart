from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import psutil
import pytest

from yara_app.fileio import atomic_write_text, read_json
from yara_app.native_login import browser_lease
from yara_app.profile_keys import KeyStore, ProfileProtection, ProtectionError, key_id
from yara_app.profile_vault import (
    browser_vault_status,
    enable_protection,
    encrypt_file,
    is_encrypted,
    seal_browser,
    unseal_browser,
    vault_path,
)
from yara_app.project_adapter import ProjectAdapter, WorkerStatus


@pytest.fixture
def protected(tmp_path, monkeypatch):
    monkeypatch.setenv('HEART_KEYS_DIR', str(tmp_path / 'private_keys'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    root = tmp_path / 'app'
    adapter = ProjectAdapter(root)
    adapter.ensure_runtime_files()
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(False, None))
    monkeypatch.setattr(adapter, 'get_telegram_bot_status', lambda: WorkerStatus(False, None))
    monkeypatch.setattr('yara_app.native_login.chrome_profile_running', lambda _p: False)
    browser = adapter._bot_profile_root('default') / 'browser'
    data = browser / 'user_data'
    (data / 'Default/Network').mkdir(parents=True)
    (data / 'Default/Network/Cookies').write_bytes(b'private-test-cookie-data')
    (data / 'Default/Preferences').write_text('{}')
    (data / 'Local State').write_text('{}')
    (data / '.heart-browser.json').write_text('{"channel":"chrome"}')
    return adapter, browser


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_protection_uses_persistent_user_key_and_no_plain_key_on_disk(protected):
    adapter, browser = protected
    first = enable_protection(adapter)
    identity = first['key_id']
    master, record = KeyStore().load(identity)
    assert len(master) == 32 and key_id(master) == identity
    assert browser_vault_status(browser)['sealed']
    assert not (browser / 'user_data/Default/Network/Cookies').exists()
    assert is_encrypted(vault_path(browser))
    assert 'private-test-cookie-data' not in vault_path(browser).read_bytes().decode(errors='ignore')
    key_file = KeyStore()._path(identity).read_bytes()
    assert master not in key_file and record['phrase'].encode() not in key_file
    config_raw = adapter.profiles_path.read_text()
    assert 'tiktok-heart-profiles-v1' in config_raw
    assert 'default' not in config_raw
    assert 'default' in read_json(adapter.profiles_path)
    assert enable_protection(adapter)['key_id'] == identity
    assert not first['errors']


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_browser_lease_opens_and_reseals_latest_session(protected):
    adapter, browser = protected
    enable_protection(adapter)
    with browser_lease(browser):
        cookie = browser / 'user_data/Default/Network/Cookies'
        assert cookie.read_bytes() == b'private-test-cookie-data'
        cookie.write_bytes(b'latest-cookie')
    assert not cookie.exists()
    with browser_lease(browser):
        assert cookie.read_bytes() == b'latest-cookie'
    assert browser_vault_status(browser)['sealed']


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_recovery_restores_same_key_on_clean_install_and_wrong_phrase_preserves_data(protected, tmp_path, monkeypatch):
    adapter, browser = protected
    identity = enable_protection(adapter)['key_id']
    manager = ProfileProtection(adapter.base_dir)
    bundle = manager.recovery()
    package = json.loads(Path(bundle['path']).read_text())
    original = KeyStore().load(identity)[0]
    assert bundle['phrase'] not in Path(bundle['path']).read_text()
    vault_before = vault_path(browser).read_bytes()
    key_before = KeyStore()._path(identity).read_bytes()
    with pytest.raises(ProtectionError):
        manager.restore(package, '000000 ' * 8)
    assert KeyStore()._path(identity).read_bytes() == key_before
    assert vault_path(browser).read_bytes() == vault_before
    monkeypatch.setenv('HEART_KEYS_DIR', str(tmp_path / 'clean_windows_keys'))
    with pytest.raises(ProtectionError):
        unseal_browser(browser)
    assert not (browser / 'user_data/Default').exists()
    with pytest.raises(ProtectionError):
        manager.initialize()
    assert manager.restore(package, bundle['phrase'])['key_id'] == identity
    assert KeyStore().load(identity)[0] == original
    with browser_lease(browser):
        assert (browser / 'user_data/Default/Network/Cookies').read_bytes() == b'private-test-cookie-data'
    # A new checkout on the same Windows account also reuses this exact key.
    fresh = ProfileProtection(tmp_path / 'fresh_checkout')
    assert fresh.initialize() == identity


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_missing_or_corrupt_key_never_generates_replacement(protected):
    adapter, browser = protected
    identity = enable_protection(adapter)['key_id']
    master = KeyStore()._path(identity)
    master.write_bytes(b'corrupt-protected-key')
    original = vault_path(browser).read_bytes()
    for operation in (lambda: KeyStore().ensure(identity), lambda: KeyStore().ensure(),
                      lambda: unseal_browser(browser), lambda: adapter.get_profiles()):
        with pytest.raises(ProtectionError):
            operation()
    assert master.read_bytes() == b'corrupt-protected-key'
    assert vault_path(browser).read_bytes() == original


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_corrupted_archive_is_never_extracted_or_replaced_with_blank_profile(protected):
    adapter, browser = protected
    enable_protection(adapter)
    path = vault_path(browser)
    data = bytearray(path.read_bytes())
    data[-20] ^= 1
    path.write_bytes(data)
    with pytest.raises(ProtectionError), browser_lease(browser):
        pytest.fail('corrupt profile was opened')
    assert path.read_bytes() == data
    assert not (browser / 'user_data/Default').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_open_journal_preserves_newer_working_session_after_crash(protected):
    adapter, browser = protected
    enable_protection(adapter)
    unseal_browser(browser)
    cookie = browser / 'user_data/Default/Network/Cookies'
    cookie.write_bytes(b'newer-than-archive')
    unseal_browser(browser)
    assert cookie.read_bytes() == b'newer-than-archive'
    seal_browser(browser)
    unseal_browser(browser)
    assert cookie.read_bytes() == b'newer-than-archive'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_write_failure_keeps_plain_session_intact(protected, monkeypatch):
    adapter, browser = protected
    enable_protection(adapter)
    unseal_browser(browser)
    cookie = browser / 'user_data/Default/Network/Cookies'
    cookie.write_bytes(b'must-survive')
    def fail(*_args):
        raise OSError('simulated disk error')
    monkeypatch.setattr('yara_app.profile_vault.encrypt_file', fail)
    with pytest.raises(OSError):
        seal_browser(browser)
    assert cookie.read_bytes() == b'must-survive'
    assert json.loads((browser / '.vault-open.json').read_text())['phase'] == 'open'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_partial_sealing_recovers_from_verified_new_archive(protected):
    adapter, browser = protected
    enable_protection(adapter)
    (browser / 'user_data').mkdir()
    (browser / '.vault-open.json').write_text('{"phase":"sealing"}')
    unseal_browser(browser)
    assert (browser / 'user_data/Default/Network/Cookies').read_bytes() == b'private-test-cookie-data'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
@pytest.mark.parametrize('name', ['../escape', 'user_data/../../escape', 'C:/escape', 'user_data/CON'])
def test_unsafe_authenticated_zip_cannot_escape_profile(protected, name):
    adapter, browser = protected
    enable_protection(adapter)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(name, b'unsafe')
    encrypt_file(data, vault_path(browser), ProfileProtection(adapter.base_dir).key(), 'browser:default')
    with pytest.raises(ProtectionError):
        unseal_browser(browser)
    assert not (adapter.base_dir / 'escape').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_new_and_existing_auth_backups_are_encrypted_and_restore_readable(protected):
    adapter, browser = protected
    before = adapter.create_auth_backup('default')
    enable_protection(adapter)
    assert is_encrypted(before['path'])
    backup = adapter.create_auth_backup('default')
    assert is_encrypted(backup['path'])
    assert adapter.validate_auth_backup('default', backup['name'], deep=True)['ok']
    assert browser_vault_status(browser)['sealed']  # Backing up did not expose it.
    with browser_lease(browser):
        (browser / 'user_data/Default/Network/Cookies').write_bytes(b'changed')
    adapter.restore_auth_backup('default', backup['name'])
    with browser_lease(browser):
        assert (browser / 'user_data/Default/Network/Cookies').read_bytes() == b'private-test-cookie-data'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_settings_edits_remain_encrypted_and_live_readiness_survives_sealing(protected):
    adapter, browser = protected
    enable_protection(adapter)
    adapter.create_profile('second')
    adapter.add_target('second', 'A friend', '@dummy_recipient')
    assert 'dummy_recipient' not in adapter.profiles_path.read_text()
    assert 'dummy_recipient' in str(read_json(adapter.profiles_path))
    live = adapter.browser_profile_live_status('default')
    assert live['cookies_exists'] and live['default_profile_exists']
    assert live['browser_channel'] == 'chrome' and live['sealed']
    assert not (browser / 'user_data/Default').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_backup_list_does_not_decrypt_large_archives_during_ui_refresh(protected, monkeypatch):
    adapter, _browser = protected
    enable_protection(adapter)
    backup = adapter.create_auth_backup('default')
    monkeypatch.setattr('yara_app.profile_vault.decrypt_file', lambda *_a: pytest.fail('UI decrypted backup'))
    assert adapter.validate_auth_backup('default', backup['name'], deep=False)['encrypted']
    assert adapter.browser_profile_summary('default')['latest_auth_backups']


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_hidden_worker_process_opens_and_reseals_protected_profile(protected, monkeypatch):
    adapter, browser = protected
    enable_protection(adapter)
    script = adapter.main_script_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        'from pathlib import Path\nfrom yara_app.native_login import browser_lease\n'
        'b=Path.cwd()/"profiles/default/browser"\n'
        'with browser_lease(b):\n'
        '    (b/"user_data/Default/Network/Cookies").write_bytes(b"changed-by-dummy-worker")\n'
        '(Path.cwd()/"dummy_completed").write_text("done")\n')
    source_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PYTHONPATH', str(source_root))
    status = adapter._start_worker_profile('default')
    psutil.Process(status.pid).wait(timeout=15)
    assert (adapter.base_dir / 'dummy_completed').read_text() == 'done'
    assert browser_vault_status(browser)['sealed']
    with browser_lease(browser):
        assert (browser / 'user_data/Default/Network/Cookies').read_bytes() == b'changed-by-dummy-worker'


def test_normal_settings_stay_compatible_when_protection_disabled(tmp_path):
    path = tmp_path / 'control/profiles.json'
    atomic_write_text(path, '{"normal":[]}')
    assert json.loads(path.read_text()) == {'normal': []}

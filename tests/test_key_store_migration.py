from __future__ import annotations

import os

import pytest

from yara_app.profile_keys import KeyStore, ProtectionError


@pytest.fixture
def legacy_key(tmp_path, monkeypatch):
    legacy = tmp_path / 'local' / 'TikTokHeart' / 'Keys'
    monkeypatch.setenv('HEART_KEYS_DIR', str(legacy))
    monkeypatch.setattr('yara_app.profile_keys.SCRYPT_N', 1024)
    old = KeyStore()
    identity = old.ensure()
    original, _ = old.load(identity)
    blob = old._path(identity).read_bytes()
    monkeypatch.delenv('HEART_KEYS_DIR')
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'user'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    return legacy, identity, original, blob


def test_default_store_does_not_depend_on_redirected_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv('HEART_KEYS_DIR', raising=False)
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'user'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'package_local'))
    packaged = KeyStore().folder
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'real_local'))
    assert KeyStore().folder == packaged == tmp_path / 'user' / '.tiktok-heart' / 'keys'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_legacy_migration_keeps_exact_key_blob_and_active_choice(legacy_key):
    legacy, identity, original, blob = legacy_key
    store = KeyStore()
    assert store.load(identity)[0] == original
    assert store._path(identity).read_bytes() == blob
    assert (legacy / (identity + '.dpapi')).read_bytes() == blob
    assert store.ensure() == identity
    assert KeyStore().load(identity)[0] == original


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_migration_never_replaces_existing_unreadable_key(legacy_key):
    _legacy, identity, _original, _blob = legacy_key
    store = KeyStore()
    store.folder.mkdir(parents=True)
    store._path(identity).write_bytes(b'damaged-canonical-key')
    with pytest.raises(ProtectionError):
        store.load(identity)
    assert store._path(identity).read_bytes() == b'damaged-canonical-key'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_broken_legacy_active_never_generates_replacement(legacy_key):
    legacy, _identity, _original, _blob = legacy_key
    (legacy / 'active.json').write_text('{bad', encoding='utf-8')
    store = KeyStore()
    with pytest.raises(ProtectionError):
        store.ensure()
    assert not list(store.folder.glob('*.dpapi'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_explicit_custom_store_is_not_silently_migrated(legacy_key, tmp_path, monkeypatch):
    _legacy, identity, _original, _blob = legacy_key
    custom = tmp_path / 'explicit'
    monkeypatch.setenv('HEART_KEYS_DIR', str(custom))
    store = KeyStore()
    assert store.folder == custom
    with pytest.raises(ProtectionError):
        store.load(identity)
    assert not store._path(identity).exists()

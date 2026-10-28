"""Authenticated browser archives. Plain working files exist only while in use.

An open journal makes working files authoritative after a crash. A sealing or
restoring journal makes the verified archive authoritative. Never replace a
working session with an older archive just because the process was interrupted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from yara_app.fileio import atomic_write_text, file_lock
from yara_app.profile_keys import KeyStore, ProfileProtection, ProtectionError, key_id

logger = logging.getLogger(__name__)
MAGIC = b'TTHV001\n'
CHUNK = 1024 * 1024
LIMIT = 8 * 1024 ** 3
JOURNAL = '.vault-open.json'
STATUS = '.vault-status.json'
EXCLUDED = {JOURNAL, STATUS, '.vault-error.json', '.browser_session.lock', '.session_import.lock'}


@contextmanager
def private_temp():
    folder = KeyStore().folder / 'work'
    folder.mkdir(parents=True, exist_ok=True)
    # Auto-deleted even on normal exceptions; no plaintext under Downloads or
    # the Git checkout. Windows stores this under the user's Local AppData.
    with tempfile.TemporaryFile(dir=folder) as handle:
        yield handle


def is_encrypted(path):
    with Path(path).open('rb') as handle:
        return handle.read(len(MAGIC)) == MAGIC


def encrypt_file(plain, destination, key, purpose):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    destination = Path(destination)
    nonce = secrets.token_bytes(12)
    header = MAGIC + key_id(key).encode() + nonce
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header + purpose.encode())
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix='.vault_', suffix='.tmp')
    temporary = Path(name)
    try:
        plain.seek(0)
        with os.fdopen(fd, 'wb') as output:
            output.write(header)
            while block := plain.read(CHUNK):
                output.write(cipher.update(block))
            output.write(cipher.finalize())
            output.write(cipher.tag)
            output.flush()
            os.fsync(output.fileno())
        # Verify the saved ciphertext and tag before publishing it. Compare
        # hashes so a write error cannot lead to deleting the original files.
        with private_temp() as verified:
            decrypt_file(temporary, verified, key, purpose)
            plain.seek(0)
            if hashlib.file_digest(plain, 'sha256').digest() != hashlib.file_digest(verified, 'sha256').digest():
                raise ProtectionError('Проверка зашифрованного файла не пройдена. Исходные файлы сохранены.')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def decrypt_file(source, output, key, purpose):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    try:
        with Path(source).open('rb') as handle:
            size = os.fstat(handle.fileno()).st_size
            header = handle.read(len(MAGIC) + 32 + 12)
            if (not header.startswith(MAGIC) or header[len(MAGIC):len(MAGIC) + 32] != key_id(key).encode()
                    or not len(header) + 16 <= size <= LIMIT):
                raise ValueError('header')
            handle.seek(-16, os.SEEK_END)
            tag = handle.read(16)
            cipher = Cipher(algorithms.AES(key), modes.GCM(header[-12:], tag)).decryptor()
            cipher.authenticate_additional_data(header + purpose.encode())
            handle.seek(len(header))
            remaining = size - len(header) - 16
            while remaining:
                block = handle.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError('truncated')
                output.write(cipher.update(block))
                remaining -= len(block)
            output.write(cipher.finalize())
        output.seek(0)  # No caller sees or extracts data until tag validation.
    except Exception:
        output.seek(0)
        output.truncate()
        raise ProtectionError('Профиль или ключ не прошёл проверку. Зашифрованные данные сохранены.') from None


@contextmanager
def protected_zip(root, path, purpose='auth-backup'):
    if not is_encrypted(path):
        with zipfile.ZipFile(path) as archive:
            yield archive
    else:
        with private_temp() as plain:
            decrypt_file(path, plain, ProfileProtection(root).key(), purpose)
            with zipfile.ZipFile(plain) as archive:
                yield archive


def protect_backup(root, path):
    manager = ProfileProtection(root)
    if manager.config() and not is_encrypted(path):
        with Path(path).open('rb') as plain:
            # encrypt_file replaces the destination only after it closes its
            # source. Windows cannot replace an open input, so publish below.
            temporary = Path(path).with_name(Path(path).name + '.encrypted')
            encrypt_file(plain, temporary, manager.key(), 'auth-backup')
        os.replace(temporary, path)


def manager_for_browser(browser):
    browser = Path(browser).resolve()
    if browser.name != 'browser' or browser.parent.parent.name != 'profiles':
        return None
    manager = ProfileProtection(browser.parents[2])
    return manager if manager.config() else None


def vault_path(browser):
    return Path(browser).parent / 'browser.heart-vault'


def _journal(browser):
    path = browser / JOURNAL
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        if data and data.get('phase') not in {'open', 'sealing', 'restoring'}:
            raise ValueError('phase')
        return data
    except Exception:
        raise ProtectionError('Запись защиты повреждена. Файлы сохранены; используйте восстановление.') from None


def _payload(browser):
    return [p for p in browser.iterdir() if p.name not in EXCLUDED]


def _reject_link(path):
    attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    if path.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        raise ProtectionError('Профиль содержит ссылку на другую папку. Файлы оставлены без изменений.')


def _remove_payload(browser):
    base = browser.resolve()
    for child in _payload(browser):
        _reject_link(child)
        target = child.resolve()
        if not target.is_relative_to(base) or target == base:
            raise ProtectionError('Небезопасный путь. Исходные файлы сохранены.')
        for nested in child.rglob('*') if child.is_dir() else []:
            _reject_link(nested)
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _archive_entries(archive):
    from yara_app.backups import BackupService
    infos = archive.infolist()
    if len(infos) > 100000 or sum(i.file_size for i in infos) > LIMIT:
        raise ProtectionError('Архив профиля превышает допустимый размер.')
    seen = set()
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    for info in infos:
        parts = PurePosixPath(info.filename).parts
        if (not BackupService.safe_zip_name(info.filename) or not parts or parts[0] in EXCLUDED
                or any(part.split('.')[0].upper() in reserved for part in parts)
                or stat.S_ISLNK(info.external_attr >> 16) or info.filename.casefold() in seen):
            raise ProtectionError('Небезопасный архив. Исходный профиль не изменён.')
        seen.add(info.filename.casefold())
    return infos


def unseal_browser(browser):
    browser = Path(browser).resolve()
    manager = manager_for_browser(browser)
    vault = vault_path(browser)
    if not manager or not vault.exists():
        return
    key = manager.key()
    phase = _journal(browser).get('phase')
    if phase == 'open':
        return  # Preserve the latest working session after an interrupted run.
    if phase is None and any(p.is_file() for child in _payload(browser)
                             for p in (child.rglob('*') if child.is_dir() else [child])):
        raise ProtectionError('Рядом с архивом появились новые файлы профиля. Они сохранены; автоматическая замена отменена.')
    with private_temp() as plain:
        decrypt_file(vault, plain, key, 'browser:' + browser.parent.name)
        with zipfile.ZipFile(plain) as archive:
            infos = _archive_entries(archive)
            # CRC and authentication are both checked before touching originals.
            if archive.testzip() is not None:
                raise ProtectionError('Архив профиля повреждён. Исходные файлы сохранены.')
            atomic_write_text(browser / JOURNAL, json.dumps({'phase': 'restoring'}))
            for previous in browser.parent.glob('.vault_restore_*'):
                _reject_link(previous)
                resolved = previous.resolve()
                if resolved.parent != browser.parent or not resolved.name.startswith('.vault_restore_'):
                    raise ProtectionError('Небезопасная папка восстановления. Файлы сохранены.')
                for nested in previous.rglob('*'):
                    _reject_link(nested)
                shutil.rmtree(resolved)
            staging = Path(tempfile.mkdtemp(prefix='.vault_restore_', dir=browser.parent))
            try:
                for info in infos:
                    destination = staging.joinpath(*PurePosixPath(info.filename).parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as source, destination.open('wb') as output:
                            shutil.copyfileobj(source, output, CHUNK)
                _remove_payload(browser)
                for child in staging.iterdir():
                    os.replace(child, browser / child.name)
                atomic_write_text(browser / JOURNAL, json.dumps({'phase': 'open'}))
            finally:
                resolved = staging.resolve()
                if resolved.parent == browser.parent and resolved.name.startswith('.vault_restore_'):
                    shutil.rmtree(resolved)


def seal_browser(browser):
    browser = Path(browser).resolve()
    manager = manager_for_browser(browser)
    if not manager:
        return False
    from yara_app.native_login import chrome_profile_running, uses_google_chrome
    if (browser / '.manual_login.json').exists() or chrome_profile_running(browser / 'user_data'):
        return False
    phase = _journal(browser).get('phase')
    if phase in {'sealing', 'restoring'}:
        unseal_browser(browser)
    payload = _payload(browser)
    files = []
    for child in payload:
        _reject_link(child)
        for path in child.rglob('*') if child.is_dir() else [child]:
            _reject_link(path)
            if path.is_file():
                files.append(path)
    if not files:
        return vault_path(browser).exists()
    if sum(p.stat().st_size for p in files) > LIMIT - CHUNK:
        raise ProtectionError('Профиль слишком большой для защиты. Исходные файлы сохранены.')
    data = browser / 'user_data'
    metadata = {'exists': data.exists(), 'default_profile_exists': (data / 'Default').exists(),
                'local_state_exists': (data / 'Local State').exists(),
                'preferences_exists': (data / 'Default/Preferences').exists(),
                'cookies_exists': any((data / 'Default' / f).exists() for f in ('Network/Cookies', 'Cookies')),
                'browser_channel': 'chrome' if uses_google_chrome(data) else 'chromium'}
    with private_temp() as plain:
        with zipfile.ZipFile(plain, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for path in files:
                archive.write(path, path.relative_to(browser).as_posix())
        encrypt_file(plain, vault_path(browser), manager.key(), 'browser:' + browser.parent.name)
    atomic_write_text(browser / STATUS, json.dumps(metadata))
    atomic_write_text(browser / JOURNAL, json.dumps({'phase': 'sealing'}))
    _remove_payload(browser)
    (browser / JOURNAL).unlink()
    (browser / '.vault-error.json').unlink(missing_ok=True)
    return True


def browser_vault_status(browser):
    browser = Path(browser)
    if (browser / '.vault-error.json').exists():
        return {'protected': vault_path(browser).exists(), 'sealed': False,
                'protection_error': 'Не удалось завершить защиту. Исходные файлы сохранены.'}
    if not vault_path(browser).exists():
        return {}
    try:
        metadata = json.loads((browser / STATUS).read_text())
        sealed = not _journal(browser) and not (browser / 'user_data/Default').exists()
        return {'protected': True, 'sealed': sealed, 'vault_bytes': vault_path(browser).stat().st_size,
                **({k: metadata[k] for k in ('exists', 'default_profile_exists', 'local_state_exists',
                   'preferences_exists', 'cookies_exists', 'browser_channel') if k in metadata} if sealed else {})}
    except (OSError, ValueError):
        return {'protected': True, 'sealed': False, 'protection_error': 'Проверьте состояние защиты профиля.'}


def enable_protection(adapter):
    from yara_app.native_login import LOGIN_PENDING, browser_lease, chrome_profile_running
    manager = ProfileProtection(adapter.base_dir)
    with file_lock(adapter.control_dir / 'worker_launch'):
        if adapter.get_worker_status().running or adapter.get_telegram_bot_status().running:
            raise ProtectionError('Сначала остановите отправку и управление Telegram. После защиты их можно включить снова.')
        profiles = adapter.get_profiles()
        if any((adapter._bot_profile_root(p.key) / 'browser' / LOGIN_PENDING).exists() for p in profiles):
            raise ProtectionError('Сначала завершите вход в TikTok.')
        with ExitStack() as leases:
            for profile in profiles:
                browser = adapter._bot_profile_root(profile.key) / 'browser'
                if chrome_profile_running(browser / 'user_data'):
                    raise ProtectionError('Закройте отдельные окна Chrome профилей перед включением защиты.')
                leases.enter_context(browser_lease(browser))
            with file_lock(adapter.profiles_path):
                data = adapter._read_json(adapter.profiles_path, {})
                identity = manager.initialize()
                adapter._write_json(adapter.profiles_path, data)
            for backup in (adapter.base_dir / 'backups/auth').glob('*.zip'):
                protect_backup(adapter.base_dir, backup)
        protected = []
        errors = []
        for profile in profiles:
            browser = adapter._bot_profile_root(profile.key) / 'browser'
            if browser_vault_status(browser).get('sealed'):
                protected.append(profile.key)
            elif browser_vault_status(browser).get('protection_error'):
                errors.append(profile.key)
        return {'key_id': identity, 'protected_profiles': protected, 'enabled': True, 'errors': errors}


def seal_idle_profiles(adapter, profile=None):
    from yara_app.native_login import browser_lease
    try:
        enabled = bool(ProfileProtection(adapter.base_dir).config())
    except Exception:
        logger.warning('Не удалось проверить защиту профилей; файлы сохранены.')
        return
    if not enabled:
        return
    keys = [profile] if profile else [p.key for p in adapter.get_profiles()]
    for key in keys:
        browser = adapter._bot_profile_root(key) / 'browser'
        if not browser.exists() or (vault_path(browser).exists() and not (browser / JOURNAL).exists()
                                    and not any(browser.parent.glob('.vault_restore_*'))):
            continue
        try:
            with browser_lease(browser):
                pass
        except Exception:
            logger.warning('Не удалось закрыть защиту профиля %s; файлы сохранены.', key)

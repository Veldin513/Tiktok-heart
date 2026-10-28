"""User-scoped Windows key storage and portable, phrase-protected recovery."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
from pathlib import Path

from yara_app.fileio import atomic_write_text, file_lock

CONFIG_NAME = 'profile_protection.json'
RECOVERY_FORMAT = 'tiktok-heart-recovery-v1'
CONFIG_FORMAT = 'tiktok-heart-profiles-v1'
SCRYPT_N = 2 ** 17


class ProtectionError(ValueError):
    """Safe to display: contains no plaintext, keys or recovery phrases."""


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def _unb64(value: str, size: int | None = None) -> bytes:
    result = base64.b64decode(value, validate=True)
    if size is not None and len(result) != size:
        raise ValueError('Invalid encoded length')
    return result


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:32]


def normalize_phrase(phrase: str) -> str:
    value = ''.join(phrase.split()).lower()
    if len(value) != 48 or any(c not in '0123456789abcdef' for c in value):
        raise ProtectionError('Введите секретную фразу полностью. Пробелы между группами можно оставить.')
    return value


def dpapi(value: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise ProtectionError('Хранение ключа поддерживается в Windows.')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    free = ctypes.WinDLL('kernel32', use_last_error=True).LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    # User scope; never CRYPTPROTECT_LOCAL_MACHINE. No interactive Windows UI.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ProtectionError('Windows не открыла ключ. Восстановите его файлом и секретной фразой.')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free(target.data)


def recovery_package(key: bytes, phrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    wrapping = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1).derive(normalize_phrase(phrase).encode())
    identity = key_id(key)
    data = AESGCM(wrapping).encrypt(nonce, key, (RECOVERY_FORMAT + identity).encode())
    return {'format': RECOVERY_FORMAT, 'key_id': identity, 'salt': _b64(salt),
            'nonce': _b64(nonce), 'data': _b64(data)}


def recover_key(package: dict, phrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    try:
        if package['format'] != RECOVERY_FORMAT:
            raise ValueError('format')
        salt, nonce, data = _unb64(package['salt'], 16), _unb64(package['nonce'], 12), _unb64(package['data'], 48)
        wrapping = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1).derive(normalize_phrase(phrase).encode())
        key = AESGCM(wrapping).decrypt(nonce, data, (RECOVERY_FORMAT + package['key_id']).encode())
        if key_id(key) != package['key_id']:
            raise ValueError('identity')
        return key
    except Exception:
        raise ProtectionError('Файл или секретная фраза не подходят. Данные профилей не изменены.') from None


class KeyStore:
    def __init__(self):
        custom = os.getenv('HEART_KEYS_DIR')
        local = os.getenv('LOCALAPPDATA')
        if not custom and not local:
            raise ProtectionError('Не найдена личная папка Windows для ключа.')
        self.folder = Path(custom) if custom else Path(local) / 'TikTokHeart' / 'Keys'

    def _path(self, identity):
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise ProtectionError('Идентификатор ключа повреждён. Используйте восстановление.')
        return self.folder / (identity + '.dpapi')

    def load(self, identity):
        try:
            path = self._path(identity)
            if path.stat().st_size > 65536:
                raise ValueError('size')
            data = json.loads(dpapi(base64.b64decode(path.read_bytes(), validate=True), decrypt=True))
            key = _unb64(data['key'], 32)
            if key_id(key) != identity:
                raise ValueError('identity')
            return key, data
        except Exception:
            raise ProtectionError('Ключ недоступен. Он не будет заменён новым. Откройте восстановление профилей.') from None

    def _save(self, key, phrase, package):
        identity = key_id(key)
        payload = json.dumps({'key': _b64(key), 'phrase': phrase, 'recovery': package}).encode()
        protected = _b64(dpapi(payload))
        self.folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path(identity), protected)
        atomic_write_text(self.folder / 'active.json', json.dumps({'key_id': identity}))
        return identity

    def ensure(self, identity=None):
        with file_lock(self.folder / 'key_store'):
            active = self.folder / 'active.json'
            if identity is None and active.exists():
                try:
                    identity = json.loads(active.read_text())['key_id']
                except Exception:
                    raise ProtectionError('Запись ключа повреждена. Новый ключ автоматически не создаётся.') from None
            if identity:
                key, _data = self.load(identity)
                return key_id(key)
            key = secrets.token_bytes(32)
            raw = secrets.token_hex(24)
            phrase = ' '.join(raw[i:i + 6] for i in range(0, 48, 6))
            return self._save(key, phrase, recovery_package(key, phrase))

    def restore(self, package, phrase):
        key = recover_key(package, phrase)
        with file_lock(self.folder / 'key_store'):
            return self._save(key, ' '.join(normalize_phrase(phrase)[i:i + 6] for i in range(0, 48, 6)), package)


class ProfileProtection:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'control' / CONFIG_NAME

    def config(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            KeyStore()._path(data['key_id'])
            if data.get('version') != 1 or data.get('enabled') is not True:
                raise ValueError('format')
            return data
        except Exception:
            raise ProtectionError('Настройки защиты повреждены. Используйте восстановление профилей.') from None

    def key(self):
        identity = self.config().get('key_id')
        if not identity:
            raise ProtectionError('Защита ещё не включена.')
        return KeyStore().load(identity)[0]

    def status(self):
        try:
            config = self.config()
            identity = config.get('key_id')
            available = False
            if identity:
                KeyStore().load(identity)
                available = True
            from yara_app.profile_vault import browser_vault_status
            problems = sum(bool(browser_vault_status(p / 'browser').get('protection_error'))
                           for p in (self.root / 'profiles').iterdir() if p.is_dir()) if (self.root / 'profiles').exists() else 0
            return {'enabled': bool(config), 'key_id': identity, 'key_available': available, 'problem_profiles': problems}
        except ProtectionError as exc:
            return {'enabled': self.path.exists(), 'key_available': False, 'error': str(exc)}

    def initialize(self):
        config = self.config()
        identity = KeyStore().ensure(config.get('key_id'))
        atomic_write_text(self.path, json.dumps({'version': 1, 'enabled': True, 'key_id': identity}))
        return identity

    def recovery(self):
        identity = self.config().get('key_id')
        if not identity:
            raise ProtectionError('Сначала включите защиту.')
        _key, data = KeyStore().load(identity)
        folder = Path.home() / 'Downloads'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'TikTokHeart-Recovery-{identity[:8]}.heart-recovery'
        atomic_write_text(path, json.dumps(data['recovery'], indent=2))
        return {'path': str(path), 'phrase': data['phrase'], 'key_id': identity}

    def restore(self, package, phrase):
        try:
            expected = json.loads(self.path.read_text()).get('key_id') if self.path.exists() else None
        except (OSError, ValueError):
            expected = None
        profiles = self.root / 'control/profiles.json'
        raw = json.loads(profiles.read_text(encoding='utf-8-sig')) if profiles.exists() else {}
        if raw.get('format') == CONFIG_FORMAT:
            expected = raw.get('key_id')
        if expected and package.get('key_id') != expected:
            raise ProtectionError('Этот файл восстановления относится к другому ключу. Профили не изменены.')
        key = recover_key(package, phrase)
        if raw.get('format') == CONFIG_FORMAT:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            try:
                json.loads(AESGCM(key).decrypt(_unb64(raw['nonce'], 12), _unb64(raw['data']), CONFIG_FORMAT.encode()))
            except Exception:
                raise ProtectionError('Файл профилей не прошёл проверку. Исходные данные сохранены.') from None
        identity = KeyStore().restore(package, phrase)
        atomic_write_text(self.path, json.dumps({'version': 1, 'enabled': True, 'key_id': identity}))
        return {'key_id': identity, 'restored': True}


def decode_profile_config(path, data):
    if not isinstance(data, dict) or data.get('format') != CONFIG_FORMAT:
        return data
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        manager = ProfileProtection(Path(path).parent.parent)
        if manager.config().get('key_id') != data['key_id']:
            raise ValueError('identity')
        plain = AESGCM(manager.key()).decrypt(_unb64(data['nonce'], 12), _unb64(data['data']), CONFIG_FORMAT.encode())
        return json.loads(plain)
    except Exception:
        raise ProtectionError('Не удалось расшифровать список профилей. Восстановите прежний ключ; данные сохранены.') from None


def encode_profile_config(path, text):
    path = Path(path)
    if path.name != 'profiles.json' or path.parent.name != 'control':
        return text
    manager = ProfileProtection(path.parent.parent)
    config = manager.config()
    if not config:
        return text
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    data = AESGCM(manager.key()).encrypt(nonce, text.encode('utf-8'), CONFIG_FORMAT.encode())
    return json.dumps({'format': CONFIG_FORMAT, 'key_id': config['key_id'], 'nonce': _b64(nonce), 'data': _b64(data)})

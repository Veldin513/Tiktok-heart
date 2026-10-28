"""Atomic writes and stable OS locks shared by the desktop, worker and bot."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

_locks: dict[str, threading.RLock] = {}
_registry = threading.Lock()
_held = threading.local()


def read_text(path: Path, *, encoding: str = 'utf-8-sig') -> str:
    for attempt in range(40):
        try:
            return path.read_text(encoding=encoding)
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.025)
    raise AssertionError('unreachable')


def read_json(path: Path, default=None):
    try:
        data = json.loads(read_text(path))
        if path.name == 'profiles.json' and path.parent.name == 'control':
            from yara_app.profile_keys import decode_profile_config
            return decode_profile_config(path, data)
        return data
    except FileNotFoundError:
        return default


def atomic_write_text(path: Path, text: str, *, encoding: str = 'utf-8') -> None:
    if path.name == 'profiles.json' and path.parent.name == 'control':
        from yara_app.profile_keys import encode_profile_config
        text = encode_profile_config(path, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    temporary = path.with_name(os.path.basename(name))
    try:
        with os.fdopen(fd, 'w', encoding=encoding, newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(40):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.025)
    finally:
        with suppress(OSError):
            temporary.unlink()


@contextmanager
def file_lock(path: Path, *, timeout: float = 10):
    """Reentrant per thread, exclusive across processes; never delete a live lock."""
    key = str(path.resolve()).casefold() if sys.platform == 'win32' else str(path.resolve())
    with _registry:
        mutex = _locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not mutex.acquire(timeout=max(0, timeout)):
        raise TimeoutError(f'Файл {path.name} занят другой операцией.')
    owned = getattr(_held, 'paths', set())
    if key in owned:
        try:
            yield
        finally:
            mutex.release()
        return
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Файл {path.name} занят другой операцией.') from None
                time.sleep(0.025)
        _held.paths = owned | {key}
        yield
    finally:
        _held.paths = owned
        if fd is not None:
            os.close(fd)
        mutex.release()

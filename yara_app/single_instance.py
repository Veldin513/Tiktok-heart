from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path

_APP_PROCESS_MARKERS = {
    'app_shell_server': ('app_shell\\server.py', 'app_shell/server.py'),
}
_APP_PROCESS_NAMES = {
    'app_shell_server': ('python.exe', 'pythonw.exe', 'python'),
}


def _safe_process_value(proc: object, attr: str) -> str:
    try:
        value = getattr(proc, attr)()
    except Exception:
        return ''
    if isinstance(value, (list, tuple)):
        return ' '.join(str(item) for item in value)
    return str(value or '')


def _process_matches_app(proc: object, app_name: str) -> bool:
    markers = _APP_PROCESS_MARKERS.get(app_name)
    if not markers:
        return True
    name = _safe_process_value(proc, 'name').lower()
    allowed_names = _APP_PROCESS_NAMES.get(app_name, ())
    process_text = ' '.join(
        [
            name,
            _safe_process_value(proc, 'exe'),
            _safe_process_value(proc, 'cmdline'),
        ]
    ).lower()
    normalized_text = process_text.replace('/', '\\')
    if any(marker.replace('/', '\\').lower() in normalized_text for marker in markers):
        return True
    return bool(name and name in allowed_names)


def _pid_running(pid: int, app_name: str | None = None) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            status = getattr(proc, 'status', lambda: None)()
            if status == getattr(psutil, 'STATUS_ZOMBIE', 'zombie'):
                return False
            return not (app_name and not _process_matches_app(proc, app_name))
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


class SingleInstanceGuard:
    def __init__(self, lock_path: Path, fd: int) -> None:
        self.lock_path = Path(lock_path)
        self._fd = fd
        self._released = False

    @classmethod
    def acquire(cls, lock_path: Path | str, app_name: str = 'app') -> SingleInstanceGuard | None:
        path = Path(lock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                if not cls._clear_stale_lock(path, app_name):
                    return None
                continue
            payload = {'pid': os.getpid(), 'app': app_name}
            os.write(fd, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            return cls(path, fd)
        return None

    @staticmethod
    def _clear_stale_lock(path: Path, app_name: str) -> bool:
        try:
            payload = json.loads(path.read_text(encoding='utf-8') or '{}')
        except Exception:
            payload = {}
        pid = int(payload.get('pid') or 0)
        expected_app = str(payload.get('app') or app_name)
        if _pid_running(pid, expected_app):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except Exception:
            return False
        return True

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with suppress(Exception):
            os.close(self._fd)
        with suppress(Exception):
            self.lock_path.unlink()

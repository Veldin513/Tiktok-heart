from __future__ import annotations

import json
import os
from pathlib import Path


def _pid_running(pid: int) -> bool:
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
            return status != getattr(psutil, 'STATUS_ZOMBIE', 'zombie')
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
    def acquire(cls, lock_path: Path | str, app_name: str = 'app') -> 'SingleInstanceGuard | None':
        path = Path(lock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                if not cls._clear_stale_lock(path):
                    return None
                continue
            payload = {'pid': os.getpid(), 'app': app_name}
            os.write(fd, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            return cls(path, fd)
        return None

    @staticmethod
    def _clear_stale_lock(path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding='utf-8') or '{}')
        except Exception:
            payload = {}
        pid = int(payload.get('pid') or 0)
        if _pid_running(pid):
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
        try:
            os.close(self._fd)
        except Exception:
            pass
        try:
            self.lock_path.unlink()
        except Exception:
            pass

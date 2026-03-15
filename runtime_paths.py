from __future__ import annotations

import importlib
import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

_NATIVE_PATH_CLS = type(Path('.'))
from typing import Iterable

from config import BASE_DIR


@dataclass(frozen=True)
class ProfilePaths:
    profile_name: str
    profile_root: Path
    browser_dir: Path
    user_data_dir: Path
    logs_dir: Path
    state_dir: Path
    artifacts_dir: Path
    run_lock_file: Path


@dataclass
class RunLock:
    path: Path
    acquired: bool = False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w', encoding='utf-8') as file_obj:
                file_obj.write(str(os.getpid()))
            self.acquired = True
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass
        finally:
            self.acquired = False


def build_profile_paths(active_profile: str) -> ProfilePaths:
    profile_root = BASE_DIR / 'profiles' / active_profile
    browser_dir = profile_root / 'browser'
    user_data_dir = browser_dir / 'user_data'
    logs_dir = profile_root / 'logs'
    state_dir = profile_root / 'state'
    artifacts_dir = profile_root / 'artifacts'
    run_lock_file = state_dir / 'run.lock'
    for path in (profile_root, browser_dir, user_data_dir, logs_dir, state_dir, artifacts_dir):
        path.mkdir(parents=True, exist_ok=True)
    return ProfilePaths(active_profile, profile_root, browser_dir, user_data_dir, logs_dir, state_dir, artifacts_dir, run_lock_file)


def init_auth_runtime() -> dict:
    return {
        'qr_error_code': None,
        'qr_error_text': '',
        'qr_error_notified': False,
        'qr_error_count': 0,
        'qr_error_first_seen_ts': None,
        'qr_error_last_seen_ts': None,
        'qr_opened_ts': None,
    }


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f'{hours}ч {minutes}м'
    return f'{minutes}м'


class SafeConsoleHandler(logging.StreamHandler):
    """Console handler that degrades unsupported characters instead of crashing.

    On Windows the default stdout encoding may still be cp1251/cp866 even when the
    worker output is redirected to a UTF-8 file. Standard ``StreamHandler`` then
    raises ``UnicodeEncodeError`` on emoji or stylized profile names. This handler
    retries the write with replacement characters so logging never aborts the run.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            message = self.format(record)
            encoding = getattr(self.stream, 'encoding', None) or 'utf-8'
            safe_message = message.encode(encoding, errors='replace').decode(encoding, errors='replace')
            self.stream.write(safe_message + self.terminator)
            self.flush()


def configure_logging(paths: ProfilePaths) -> logging.Logger:
    app_log_file = paths.logs_dir / 'tiktok_bot.log'
    auth_log_file = paths.logs_dir / 'auth_debug.log'

    log_format = '%(asctime)s [%(levelname)s] %(message)s'
    date_format = '%d.%m.%Y %H:%M:%S'
    formatter = logging.Formatter(log_format, datefmt=date_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    app_file_handler = RotatingFileHandler(app_log_file, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    app_file_handler.setFormatter(formatter)
    console_handler = SafeConsoleHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(app_file_handler)
    root_logger.addHandler(console_handler)

    auth_logger = logging.getLogger('auth_debug')
    auth_logger.setLevel(logging.INFO)
    auth_logger.handlers.clear()
    auth_logger.propagate = False
    auth_file_handler = RotatingFileHandler(auth_log_file, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    auth_file_handler.setFormatter(formatter)
    auth_logger.addHandler(auth_file_handler)
    return logging.getLogger(__name__)


def _unique_existing(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            result.append(path)
    return result


def candidate_site_packages() -> list[Path]:
    candidates: list[Path] = []
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    major_minor = f"{sys.version_info.major}{sys.version_info.minor}"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = _NATIVE_PATH_CLS(local_app_data)
        for arch in ("64", "32"):
            candidates.append(base / "Python" / f"pythoncore-{version}-{arch}" / "Lib" / "site-packages")
            candidates.append(base / "Programs" / "Python" / f"Python{major_minor}" / "Lib" / "site-packages")
    home = _NATIVE_PATH_CLS(os.path.expanduser('~'))
    for base in (home / "AppData" / "Local", home / ".local"):
        for arch in ("64", "32"):
            candidates.append(base / "Python" / f"pythoncore-{version}-{arch}" / "Lib" / "site-packages")
    return _unique_existing(candidates)


def bootstrap_site_packages() -> list[str]:
    added: list[str] = []
    for path in candidate_site_packages():
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
            added.append(text)
    return added


def extend_env_with_site_packages(env: dict[str, str] | None = None) -> dict[str, str]:
    payload = dict(env or os.environ)
    candidates = [str(path) for path in candidate_site_packages()]
    if not candidates:
        return payload
    existing = [item for item in payload.get("PYTHONPATH", "").split(os.pathsep) if item]
    merged: list[str] = []
    for item in candidates + existing:
        if item and item not in merged:
            merged.append(item)
    payload["PYTHONPATH"] = os.pathsep.join(merged)
    return payload

def bootstrap_optional_dependencies(names: Iterable[str] = ('pystray', 'PIL', 'psutil')) -> None:
    """Try to import optional packages; if missing, extend sys.path and retry."""
    missing = [name for name in names if not _try_import(name)]
    if not missing:
        return
    bootstrap_site_packages()
    for name in missing:
        _try_import(name)


def module_available(name: str) -> bool:
    """Return True if the named module can be imported (after bootstrapping paths)."""
    bootstrap_optional_dependencies((name,))
    return _try_import(name)


def _try_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False

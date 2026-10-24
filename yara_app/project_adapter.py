from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from yara_app.backups import BackupService
from yara_app.native_login import (
    LOGIN_PENDING,
    browser_lease,
    chrome_profile_running,
    find_google_chrome,
    mark_google_chrome,
    uses_google_chrome,
    verify_chrome_session,
)
from yara_app.performance import PerformanceService
from yara_app.runtime_paths import browser_profile_needs_recovery, extend_env_with_site_packages
from yara_app.scheduler import WorkerScheduleService
from yara_app.ttbot.models import legacy_safe_filename, safe_name_key

_PROFILE_EDIT_LOCK = RLock()

try:
    import psutil
except Exception:  # noqa: BLE001
    psutil = None

CONTROL_DIR_NAME = "control"
CONTROL_STATE_NAME = "control_state.json"
PROFILES_NAME = "profiles.json"
MESSAGE_POOL_NAME = "message_pool.txt"
LOG_NAME = "tiktok_bot.log"
WORKER_STDOUT_NAME = "tiktok_worker.out.log"
DEFAULT_MAIN_SCRIPT = "yara_app/tiktok_checker.py"
DEFAULT_TELEGRAM_SCRIPT = "yara_app/telegram_control_bot.py"
LEGACY_MAIN_SCRIPT = "tiktok_checker.py"
LEGACY_TELEGRAM_SCRIPT = "telegram_control_bot.py"
TELEGRAM_CONFIG_NAME = "telegram_bot_v2.json"
LAUNCHER_LOG_NAME = "launcher.log"
TELEGRAM_LOG_NAME = "telegram_control_bot_v2.log"
APP_SHELL_PERF_LOG_NAME = "app_shell_perf.log"
TELEGRAM_LOCK_NAME = "telegram_bot_v2.lock"
COMMON_LOGS_DIR_NAME = "logs"
RUN_HISTORY_READ_BYTES = 512 * 1024
AUTH_DEBUG_LOG_NAME = "auth_debug.log"
WORKER_SCHEDULE_TASK_NAME = "Yara TikTok Worker"
TIKTOK_AUTH_COOKIE_DOMAINS = (
    "tiktok.com",
    "tiktokv.com",
    "tiktokcdn.com",
    "ttwstatic.com",
    "bytedance.com",
    "bytedance.net",
    "byteoversea.com",
    "bytefcdn-oversea.com",
    "ibytedtos.com",
    "isnssdk.com",
    "musical.ly",
    "pangle.io",
    "snssdk.com",
    "ttlivecdn.com",
    "ttwebview.com",
)
TIKTOK_STORAGE_TOKENS = (
    "tiktok",
    "byteoversea",
    "ibytedtos",
    "musical",
)
CHROMIUM_COMPACT_DIRS = (
    "Cache",
    "Code Cache",
    "Service Worker",
    "Extensions",
    "Local Extension Settings",
    "Extension State",
    "DNR Extension Rules",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "ShaderCache",
    "GrShaderCache",
    "Shared Dictionary",
    "OptimizationGuidePredictionModels",
    "component_crx_cache",
    "Crashpad",
)
CHROMIUM_COMPACT_FILES = (
    "History",
    "History-journal",
    "Visited Links",
    "Top Sites",
    "Top Sites-journal",
    "Favicons",
    "Favicons-journal",
    "Media History",
    "Media History-journal",
    "Network Action Predictor",
    "Network Action Predictor-journal",
    "Shortcuts",
    "Shortcuts-journal",
)


@dataclass(slots=True)
class ProfileEntry:
    key: str
    label: str
    enabled: bool = True
    active: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerStatus:
    running: bool
    pid: int | None
    started_at: float | None = None
    command: list[str] | None = None


class ProjectAdapter:
    """Project runtime adapter for the web shell, worker and Telegram control.

    The adapter intentionally owns all process-management responsibilities so the
    UI does not need to know how worker/Telegram processes are started,
    discovered, or stopped on each platform.
    """

    DEFAULT_MESSAGE_POOL = "❤️\n"
    DEFAULT_COOLDOWN_HOURS = 12

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = self._discover_project_root(base_dir)
        self.control_dir = self.base_dir / CONTROL_DIR_NAME
        self.control_state_path = self.control_dir / CONTROL_STATE_NAME
        self.profiles_path = self.control_dir / PROFILES_NAME
        self.message_pool_path = self.base_dir / MESSAGE_POOL_NAME
        self.main_script_path = self._runtime_script_path(DEFAULT_MAIN_SCRIPT, LEGACY_MAIN_SCRIPT)
        self.telegram_script_path = self._runtime_script_path(DEFAULT_TELEGRAM_SCRIPT, LEGACY_TELEGRAM_SCRIPT)
        self.telegram_config_path = self.control_dir / TELEGRAM_CONFIG_NAME
        self.telegram_lock_path = self.control_dir / TELEGRAM_LOCK_NAME
        self.common_logs_dir = self.base_dir / COMMON_LOGS_DIR_NAME
        self.windows_scripts_dir = self.base_dir / "scripts" / "windows"
        self.worker_schedule_register_script_path = self._helper_script_path("register_worker_schedule.ps1")
        self.worker_schedule_unregister_script_path = self._helper_script_path("unregister_worker_schedule.ps1")
        self._dir_size_cache: dict[str, tuple[int, float, int]] = {}
        self._chrome_profiles_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._dependency_report_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self.backups = BackupService(self)
        self.performance_reports = PerformanceService(self)
        self.scheduler = WorkerScheduleService(self, task_name=WORKER_SCHEDULE_TASK_NAME)
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.common_logs_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_runtime_paths()

    def _helper_script_path(self, name: str) -> Path:
        primary = self.windows_scripts_dir / name
        if primary.exists():
            return primary
        return self.base_dir / name

    def _runtime_script_path(self, primary_name: str, legacy_name: str) -> Path:
        primary = self.base_dir / primary_name
        if primary.exists():
            return primary
        legacy = self.base_dir / legacy_name
        if legacy.exists():
            return legacy
        return primary

    @staticmethod
    def _discover_project_root(base_dir: Path | str | None) -> Path:
        if base_dir is not None:
            return Path(base_dir).resolve()

        current = Path(__file__).resolve().parent
        candidates = [current, *current.parents]
        markers = (DEFAULT_MAIN_SCRIPT, LEGACY_MAIN_SCRIPT, CONTROL_DIR_NAME, MESSAGE_POOL_NAME)
        for candidate in candidates:
            score = sum((candidate / marker).exists() for marker in markers)
            if score >= 2:
                return candidate
        return current

    def _read_active_profile_name_quick(self) -> str:
        state = self._read_json(self.control_state_path, {})
        if isinstance(state, dict):
            active_name = str(state.get("active_profile") or "").strip()
            if active_name:
                return active_name
        profiles = self._read_json(self.profiles_path, {})
        if isinstance(profiles, dict) and profiles:
            return str(next(iter(profiles.keys())))
        return "default"

    def _profile_logs_dir(self, profile_name: str | None = None) -> Path:
        profile_key = str(profile_name or self._read_active_profile_name_quick() or "default")
        path = self.base_dir / "profiles" / profile_key / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _refresh_runtime_paths(self, profile_name: str | None = None) -> None:
        profile_key = str(profile_name or self._read_active_profile_name_quick() or "default")
        self.active_profile_logs_dir = self._profile_logs_dir(profile_key)
        self.log_path = self.active_profile_logs_dir / LOG_NAME
        self.auth_debug_log_path = self.active_profile_logs_dir / AUTH_DEBUG_LOG_NAME
        self.worker_stdout_path = self.active_profile_logs_dir / WORKER_STDOUT_NAME
        self.launcher_log_path = self.common_logs_dir / LAUNCHER_LOG_NAME
        self.telegram_log_path = self.common_logs_dir / TELEGRAM_LOG_NAME
        self.app_shell_perf_log_path = self.common_logs_dir / APP_SHELL_PERF_LOG_NAME

    def _append_file_contents(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        payload = src.read_text(encoding="utf-8", errors="replace")
        if not payload:
            with suppress(Exception):
                src.unlink()
            return
        prefix = ""
        if dst.exists() and dst.stat().st_size > 0:
            prefix = "\n"
        with dst.open("a", encoding="utf-8") as handle:
            handle.write(prefix + payload)
        with suppress(Exception):
            src.unlink()

    def _migrate_legacy_log_files(self) -> None:
        mapping = {
            self.base_dir / LOG_NAME: self.log_path,
            self.base_dir / WORKER_STDOUT_NAME: self.worker_stdout_path,
            self.base_dir / LAUNCHER_LOG_NAME: self.launcher_log_path,
            self.base_dir / TELEGRAM_LOG_NAME: self.telegram_log_path,
        }
        for src, dst in mapping.items():
            if not src.exists() or src.resolve() == dst.resolve():
                continue
            try:
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.replace(dst)
                elif src.stat().st_size > 0:
                    self._append_file_contents(src, dst)
                else:
                    src.unlink()
            except Exception:
                continue

    def get_active_profile_logs_dir(self) -> Path:
        self._refresh_runtime_paths()
        return self.active_profile_logs_dir

    def get_common_logs_dir(self) -> Path:
        self.common_logs_dir.mkdir(parents=True, exist_ok=True)
        return self.common_logs_dir

    def _default_state(self, first_profile: str) -> dict[str, Any]:
        return {
            "active_profile": first_profile,
            "cooldown_hours": self.DEFAULT_COOLDOWN_HOURS,
            "dry_run": False,
            "paused": False,
            "stop_requested": False,
            "last_run_pid": None,
            "last_run_started_at": None,
            "telegram_bot_pid": None,
            "telegram_bot_started_at": None,
        }

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_runtime_files(self) -> None:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.common_logs_dir.mkdir(parents=True, exist_ok=True)

        profiles = self._read_json(self.profiles_path, None)
        if not isinstance(profiles, dict):
            profiles = {"default": []}
            self._write_json(self.profiles_path, profiles)

        first_profile = next(iter(profiles.keys()), "default")
        state = self._read_json(self.control_state_path, None)
        defaults = self._default_state(first_profile)
        if not isinstance(state, dict):
            self._write_json(self.control_state_path, defaults)
        else:
            changed = False
            for key, value in defaults.items():
                if key not in state:
                    state[key] = value
                    changed = True
            if changed:
                self._write_json(self.control_state_path, state)

        if not self.message_pool_path.exists():
            self.message_pool_path.write_text(self.DEFAULT_MESSAGE_POOL, encoding="utf-8")

        self._refresh_runtime_paths(first_profile if first_profile else None)
        self._migrate_legacy_log_files()
        for path in self.log_files().values():
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

    def log_files(self) -> dict[str, Path]:
        self._refresh_runtime_paths()
        return {
            "log": self.log_path,
            "auth_debug": self.auth_debug_log_path,
            "worker_stdout": self.worker_stdout_path,
            "launcher_log": self.launcher_log_path,
            "telegram_log": self.telegram_log_path,
            "app_shell_perf": self.app_shell_perf_log_path,
        }

    def app_shell_performance_summary(self) -> dict[str, Any]:
        return self.performance_reports.app_shell_summary()

    def worker_phase_summary(self, profile_key: str | None = None) -> dict[str, Any]:
        return self.performance_reports.worker_phase_summary(profile_key)

    def performance_summary(self, profile_key: str | None = None) -> dict[str, Any]:
        return self.performance_reports.summary(profile_key)

    def validate_project(self) -> dict[str, Any]:
        self.ensure_runtime_files()
        critical_errors: list[str] = []
        warnings: list[str] = []

        if not self.main_script_path.exists():
            critical_errors.append(f"Не найден основной скрипт: {self.main_script_path}")

        if not self.telegram_script_path.exists():
            warnings.append(f"Не найден скрипт Telegram-бота: {self.telegram_script_path}")

        profiles_raw = self._read_json(self.profiles_path, None)
        if not isinstance(profiles_raw, (dict, list)):
            critical_errors.append(f"Некорректный profiles.json: {self.profiles_path}")

        state_raw = self._read_json(self.control_state_path, None)
        if not isinstance(state_raw, dict):
            critical_errors.append(f"Некорректный control_state.json: {self.control_state_path}")

        if self.telegram_config_path.exists():
            config = self._read_json(self.telegram_config_path, None)
            if not isinstance(config, dict):
                critical_errors.append(f"Некорректный JSON конфига Telegram: {self.telegram_config_path}")
            else:
                token = str(config.get("token") or "").strip()
                if not token or token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
                    warnings.append("Не заполнен token в control/telegram_bot_v2.json")
        else:
            warnings.append("Не найден control/telegram_bot_v2.json")

        return {
            "ok": not critical_errors,
            "critical_errors": critical_errors,
            "warnings": warnings,
        }

    def telegram_bot_ready(self) -> tuple[bool, str]:
        if not self.telegram_config_path.exists():
            return False, f"Не найден config Telegram: {self.telegram_config_path}"
        config = self._read_json(self.telegram_config_path, None)
        if not isinstance(config, dict):
            return False, f"Некорректный JSON конфига Telegram: {self.telegram_config_path}"
        token = str(config.get("token") or "").strip()
        if not token or token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
            return False, "Не заполнен token в control/telegram_bot_v2.json"
        return True, "ok"

    def get_control_state(self) -> dict[str, Any]:
        self.ensure_runtime_files()
        state = self._read_json(self.control_state_path, {})
        if not isinstance(state, dict):
            state = {}
        profiles = self._read_json(self.profiles_path, {})
        first_profile = next(iter(profiles.keys()), "default") if isinstance(profiles, dict) and profiles else "default"
        for key, value in self._default_state(first_profile).items():
            state.setdefault(key, value)
        return state

    def update_control_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        state = self.get_control_state()
        state.update(patch)
        self._write_json(self.control_state_path, state)
        if "active_profile" in patch:
            self._refresh_runtime_paths(str(state.get("active_profile") or "default"))
            self._migrate_legacy_log_files()
        return state

    def _extract_targets(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict) and isinstance(raw.get("targets"), list):
            return [item for item in raw["targets"] if isinstance(item, dict)]
        return []

    def _profiles_from_mapping(self, payload: dict[str, Any], active_name: str) -> list[ProfileEntry]:
        items: list[ProfileEntry] = []
        for index, (key, raw) in enumerate(payload.items()):
            key_text = str(key)
            targets = self._extract_targets(raw)
            label = key_text
            supports_enabled = isinstance(raw, dict) and "enabled" in raw
            enabled = True
            if isinstance(raw, dict):
                label = str(raw.get("name") or key_text)
                if supports_enabled:
                    enabled = bool(raw.get("enabled"))
            items.append(
                ProfileEntry(
                    key=key_text,
                    label=label,
                    enabled=enabled,
                    active=key_text == active_name,
                    raw={
                        "target_count": len(targets),
                        "targets": targets,
                        "index": index,
                        "supports_enabled": supports_enabled,
                        "source": raw,
                    },
                )
            )
        return items

    def _profiles_from_sequence(self, payload: list[Any], active_name: str) -> list[ProfileEntry]:
        items: list[ProfileEntry] = []
        for index, raw in enumerate(payload):
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or raw.get("id") or raw.get("name") or f"profile_{index}")
            label = str(raw.get("name") or key)
            targets = self._extract_targets(raw)
            enabled = bool(raw.get("enabled", True))
            items.append(
                ProfileEntry(
                    key=key,
                    label=label,
                    enabled=enabled,
                    active=key == active_name,
                    raw={
                        "target_count": len(targets),
                        "targets": targets,
                        "index": index,
                        "supports_enabled": True,
                        "source": raw,
                    },
                )
            )
        return items

    def get_profiles(self) -> list[ProfileEntry]:
        payload = self._read_json(self.profiles_path, {})
        state = self.get_control_state()
        active_name = str(state.get("active_profile") or "")

        if isinstance(payload, dict):
            return self._profiles_from_mapping(payload, active_name)
        if isinstance(payload, list):
            return self._profiles_from_sequence(payload, active_name)
        return []

    def set_active_profile(self, index: int) -> ProfileEntry:
        profiles = self.get_profiles()
        if index < 0 or index >= len(profiles):
            raise IndexError("profile index out of range")
        return self.set_active_profile_key(profiles[index].key)

    def create_profile(self, profile_key: str) -> ProfileEntry:
        key = str(profile_key or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', key) or key.upper() in {
            'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10)),
        }:
            raise ValueError('Название профиля: 1–48 латинских букв, цифр, дефисов или подчёркиваний. Например: main.')
        with _PROFILE_EDIT_LOCK:
            if self.get_worker_status().running:
                raise RuntimeError('Остановите worker перед изменением профилей.')
            if any(item.key.casefold() == key.casefold() for item in self.get_profiles()):
                raise ValueError('Профиль с таким названием уже существует.')
            payload = self._read_json(self.profiles_path, {})
            if not isinstance(payload, dict):
                raise ValueError('Создание профиля требует стандартного формата profiles.json: объект профилей.')
            payload[key] = []
            self._write_json(self.profiles_path, payload)
        return next(item for item in self.get_profiles() if item.key == key)

    def add_target(self, profile_key: str, target_name: str, profile_url: str) -> dict[str, str]:
        name = str(target_name or '').strip()
        value = str(profile_url or '').strip()
        if not name or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError('Укажите имя адресата из списка чатов TikTok (до 80 символов).')
        if value.startswith('@'):
            value = 'https://www.tiktok.com/' + value
        url = urlparse(value)
        if url.scheme != 'https' or url.netloc not in ('www.tiktok.com', 'tiktok.com') or not re.fullmatch(r'/@[A-Za-z0-9._]{1,64}/?', url.path):
            raise ValueError('Укажите @username или ссылку вида https://www.tiktok.com/@username.')
        target = {'name': name, 'url': 'https://www.tiktok.com/' + url.path.strip('/')}
        with _PROFILE_EDIT_LOCK:
            if self.get_worker_status().running:
                raise RuntimeError('Остановите worker перед добавлением адресата.')
            payload = self._read_json(self.profiles_path, {})
            if not isinstance(payload, dict) or profile_key not in payload:
                raise ValueError('Сначала выберите существующий профиль.')
            raw = payload[profile_key]
            targets = self._extract_targets(raw)
            if any(str(t.get('name', '')).casefold() == name.casefold() or str(t.get('url', '')).rstrip('/').casefold() == target['url'].casefold() for t in targets):
                raise ValueError('Этот адресат уже добавлен в выбранный профиль.')
            if isinstance(raw, list):
                raw.append(target)
            elif isinstance(raw, dict) and isinstance(raw.get('targets'), list):
                raw['targets'].append(target)
            else:
                raise ValueError('Неподдерживаемый формат списка адресатов.')
            self._write_json(self.profiles_path, payload)
        return target

    def set_active_profile_key(self, profile_key: str) -> ProfileEntry:
        key = str(profile_key or "").strip()
        profiles = self.get_profiles()
        profile = next((item for item in profiles if item.key == key), None)
        if profile is None:
            raise ValueError(f"Unknown profile: {key}")
        self.update_control_state({"active_profile": profile.key})
        return next(item for item in self.get_profiles() if item.key == profile.key)

    def _google_chrome_user_data_dir(self) -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is not set; cannot find Google Chrome profile directory.")
        return Path(local_app_data) / "Google" / "Chrome" / "User Data"

    def list_google_chrome_profiles(self) -> list[dict[str, Any]]:
        cached_at, cached_profiles = self._chrome_profiles_cache
        if cached_profiles and time.time() - cached_at < 60:
            return [dict(item) for item in cached_profiles]

        root = self._google_chrome_user_data_dir()
        if not root.exists():
            return []

        local_state = self._read_json(root / "Local State", {})
        info_cache = {}
        if isinstance(local_state, dict):
            info_cache = local_state.get("profile", {}).get("info_cache", {})
            if not isinstance(info_cache, dict):
                info_cache = {}

        profiles: list[dict[str, Any]] = []
        for child in sorted(root.iterdir(), key=lambda item: (item.name != "Default", item.name.lower())):
            if not child.is_dir():
                continue
            looks_like_profile = (
                child.name == "Default"
                or child.name.lower().startswith("profile ")
                or child.name in info_cache
            )
            if not looks_like_profile or not (child / "Preferences").exists():
                continue
            cache = info_cache.get(child.name, {}) if isinstance(info_cache.get(child.name, {}), dict) else {}
            profile_name = str(cache.get("name") or child.name).strip()
            label = child.name if "@" in profile_name else (profile_name or child.name)
            try:
                mtime = child.stat().st_mtime
            except OSError:
                mtime = None
            profiles.append({
                "id": child.name,
                "label": label,
                "last_modified": mtime,
            })
        self._chrome_profiles_cache = (time.time(), [dict(item) for item in profiles])
        return profiles

    def _bot_profile_root(self, profile_key: str | None = None) -> Path:
        key = str(profile_key or self._active_profile_name() or "default").strip() or "default"
        profiles_root = (self.base_dir / "profiles").resolve()
        profile_root = (profiles_root / key).resolve()
        try:
            profile_root.relative_to(profiles_root)
        except ValueError as exc:
            raise ValueError(f"Invalid profile key: {key}") from exc
        return profile_root

    def _running_process_pids_by_name(self, process_names: set[str]) -> list[int]:
        names = {name.lower() for name in process_names}
        pids: list[int] = []
        if psutil is not None:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    proc_name = str(proc.info.get("name") or "").lower()
                    pid = int(proc.info.get("pid") or 0)
                except Exception:
                    continue
                if proc_name in names and pid > 0:
                    pids.append(pid)
            return sorted(set(pids))

        if os.name == "nt":
            for name in names:
                result = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    check=False,
                    **self._windows_subprocess_kwargs(),
                )
                output = self._decode_process_output(result.stdout or b"")
                for line in output.splitlines():
                    if not line.lower().startswith(f'"{name}"'):
                        continue
                    parts = [part.strip().strip('"') for part in line.split(",")]
                    if len(parts) > 1:
                        with suppress(ValueError):
                            pids.append(int(parts[1]))
        return sorted(set(pids))

    def _copy_existing_file(self, source: Path, destination: Path) -> bool:
        if not source.exists() or not source.is_file():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return True

    def _copy_existing_dir(self, source: Path, destination: Path) -> bool:
        if not source.exists() or not source.is_dir():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return True

    def _dir_size_bytes(self, path: Path) -> int:
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        try:
            stat = path.stat()
            cache_key = str(path.resolve())
            cache_marker = int(stat.st_mtime_ns)
            cached = self._dir_size_cache.get(cache_key)
            now = time.time()
            if cached and cached[0] == cache_marker and now - cached[1] < 15:
                return cached[2]
        except OSError:
            cache_key = ""
            cache_marker = 0
            now = time.time()
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        if cache_key:
            self._dir_size_cache[cache_key] = (cache_marker, now, total)
        return total

    def _cookie_keep_clause(self) -> tuple[str, list[str]]:
        parts: list[str] = []
        params: list[str] = []
        for domain in TIKTOK_AUTH_COOKIE_DOMAINS:
            parts.append("(lower(host_key) = ? OR lower(host_key) LIKE ?)")
            params.extend([domain, f"%.{domain}"])
        return " OR ".join(parts), params

    def _filter_tiktok_cookie_db(self, cookies_path: Path) -> dict[str, Any]:
        before = 0
        kept = 0
        con = sqlite3.connect(cookies_path)
        try:
            with suppress(sqlite3.DatabaseError):
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = int(con.execute("SELECT COUNT(*) FROM cookies").fetchone()[0])
            clause, params = self._cookie_keep_clause()
            kept = int(con.execute(f"SELECT COUNT(*) FROM cookies WHERE {clause}", params).fetchone()[0])
            con.execute(f"DELETE FROM cookies WHERE NOT ({clause})", params)
            con.commit()
            with suppress(sqlite3.DatabaseError):
                con.execute("VACUUM")
        finally:
            con.close()
        for suffix in ("-wal", "-shm"):
            with suppress(Exception):
                Path(str(cookies_path) + suffix).unlink(missing_ok=True)
        return {"before": before, "kept": kept, "path": str(cookies_path)}

    def _copy_filtered_tiktok_cookies(self, source_profile: Path, dest_profile: Path) -> dict[str, Any]:
        source_network = source_profile / "Network"
        source_cookies = source_network / "Cookies"
        if not source_cookies.exists():
            source_network = source_profile
            source_cookies = source_profile / "Cookies"
        if not source_cookies.exists():
            return {"copied": False, "before": 0, "kept": 0, "path": ""}

        dest_network = dest_profile / "Network"
        dest_network.mkdir(parents=True, exist_ok=True)
        dest_cookies = dest_network / "Cookies"
        shutil.copy2(source_cookies, dest_cookies)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source_cookies) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(dest_cookies) + suffix))

        return {"copied": True, **self._filter_tiktok_cookie_db(dest_cookies)}

    def _is_tiktok_storage_name(self, name: str) -> bool:
        normalized = name.lower()
        return any(token in normalized for token in TIKTOK_STORAGE_TOKENS)

    def _copy_tiktok_indexeddb(self, source_profile: Path, dest_profile: Path) -> list[str]:
        copied: list[str] = []
        source_indexeddb = source_profile / "IndexedDB"
        if not source_indexeddb.exists():
            return copied
        dest_indexeddb = dest_profile / "IndexedDB"
        for item in source_indexeddb.iterdir():
            if not self._is_tiktok_storage_name(item.name):
                continue
            target = dest_indexeddb / item.name
            if item.is_dir():
                self._copy_existing_dir(item, target)
            elif item.is_file():
                self._copy_existing_file(item, target)
            copied.append(item.name)
        return copied

    def _copy_tiktok_session_profile(self, source_root: Path, source_profile: Path, user_data_dir: Path) -> dict[str, Any]:
        dest_profile = user_data_dir / "Default"
        dest_profile.mkdir(parents=True, exist_ok=True)

        copied_root_files: list[str] = []
        for file_name in ("Local State", "Last Version", "First Run"):
            if self._copy_existing_file(source_root / file_name, user_data_dir / file_name):
                copied_root_files.append(file_name)

        copied_profile_files: list[str] = []
        for file_name in (
            "Preferences",
            "Secure Preferences",
            "Network Persistent State",
            "TransportSecurity",
            "Trust Tokens",
        ):
            if self._copy_existing_file(source_profile / file_name, dest_profile / file_name):
                copied_profile_files.append(file_name)

        copied_small_dirs: list[str] = []
        for dir_name in ("Local Storage", "Session Storage"):
            if self._copy_existing_dir(source_profile / dir_name, dest_profile / dir_name):
                copied_small_dirs.append(dir_name)

        cookie_result = self._copy_filtered_tiktok_cookies(source_profile, dest_profile)
        copied_indexeddb = self._copy_tiktok_indexeddb(source_profile, dest_profile)
        return {
            "copied_root_files": copied_root_files,
            "copied_profile_files": copied_profile_files,
            "copied_small_dirs": copied_small_dirs,
            "copied_indexeddb": copied_indexeddb,
            "cookies": cookie_result,
        }

    def _validate_staged_chromium_import(
        self,
        user_data_dir: Path,
        *,
        copy_mode: str,
        import_details: dict[str, Any],
    ) -> dict[str, Any]:
        """Check a staged Chrome import before it can replace a live profile."""
        default_dir = user_data_dir / "Default"
        preferences_path = default_dir / "Preferences"
        local_state_path = user_data_dir / "Local State"
        cookie_paths = [default_dir / "Network" / "Cookies", default_dir / "Cookies"]
        cookies_path = next((path for path in cookie_paths if path.is_file()), cookie_paths[0])
        errors: list[str] = []

        if not default_dir.is_dir():
            errors.append("В staged-профиле отсутствует папка Default.")
        if not preferences_path.is_file():
            errors.append("В staged-профиле отсутствуют настройки браузера.")
        if not local_state_path.is_file():
            errors.append("В staged-профиле отсутствует файл Local State.")
        elif browser_profile_needs_recovery(user_data_dir):
            errors.append('Windows не может расшифровать ключ cookies этого профиля. Текущий профиль сохранён без изменений.')

        if cookies_path.is_file():
            with closing(sqlite3.connect(cookies_path)) as database:
                columns = {row[1] for row in database.execute('PRAGMA table_info(cookies)')}
                if 'encrypted_value' in columns:
                    protected = database.execute(
                        "SELECT COUNT(*) FROM cookies WHERE (host_key = 'tiktok.com' OR host_key LIKE '%.tiktok.com') "
                        'AND substr(encrypted_value, 1, 3) = ?', (b'v20',),
                    ).fetchone()[0]
                    if protected:
                        errors.append('Cookies TikTok защищены ключом приложения Chrome (v20). Простое копирование не переносит эту авторизацию. Текущий профиль сохранён без изменений.')

        cookies = import_details.get("cookies") if isinstance(import_details.get("cookies"), dict) else {}
        kept_cookies = int(cookies.get("kept") or 0)
        if copy_mode == "tiktok_session":
            if not cookies.get("copied") or not cookies_path.is_file():
                errors.append("В выбранном профиле Chrome не найдена база cookies TikTok.")
            elif kept_cookies <= 0:
                errors.append("В выбранном профиле Chrome не найдена авторизация TikTok.")

        return {
            "ok": not errors,
            "copy_mode": copy_mode,
            "default_profile_exists": default_dir.is_dir(),
            "preferences_exists": preferences_path.is_file(),
            "local_state_exists": local_state_path.is_file(),
            "cookies_exists": cookies_path.is_file(),
            "tiktok_cookies": kept_cookies,
            "errors": errors,
        }

    def _commit_staged_browser_profile(
        self,
        *,
        browser_dir: Path,
        user_data_dir: Path,
        staged_user_data_dir: Path,
        timestamp: str,
    ) -> tuple[Path | None, dict[str, Any]]:
        """Atomically promote a staged profile and restore the old one on error."""
        backup_path: Path | None = None
        old_profile_moved = False
        had_profile = user_data_dir.exists()
        rollback = {"attempted": False, "restored": False, "error": ""}
        try:
            if user_data_dir.exists():
                backup_path = browser_dir / f"user_data_before_chrome_import_{timestamp}"
                suffix = 1
                while backup_path.exists():
                    suffix += 1
                    backup_path = browser_dir / f"user_data_before_chrome_import_{timestamp}_{suffix}"
                shutil.move(str(user_data_dir), str(backup_path))
                old_profile_moved = True
            shutil.move(str(staged_user_data_dir), str(user_data_dir))
        except Exception:
            rollback["attempted"] = True
            try:
                if user_data_dir.exists() and (old_profile_moved or not had_profile):
                    self._remove_profile_path_for_compact(user_data_dir, browser_dir)
                if old_profile_moved and backup_path and backup_path.exists():
                    shutil.move(str(backup_path), str(user_data_dir))
                    rollback["restored"] = True
            except Exception as rollback_exc:  # noqa: BLE001
                rollback["error"] = str(rollback_exc)
            raise
        return backup_path, rollback

    def import_google_chrome_profile(
        self, *, chrome_profile_id: str = "Default", bot_profile_key: str | None = None,
        copy_mode: str = "tiktok_session",
    ) -> dict[str, Any]:
        profile_root = self._bot_profile_root(bot_profile_key)
        with browser_lease(profile_root / 'browser'):
            if (profile_root / 'browser' / LOGIN_PENDING).exists():
                raise RuntimeError('Сначала завершите ручной вход через отдельный Chrome.')
            return self._import_google_chrome_profile(
                chrome_profile_id=chrome_profile_id, bot_profile_key=bot_profile_key, copy_mode=copy_mode,
            )

    def _import_google_chrome_profile(
        self,
        *,
        chrome_profile_id: str = "Default",
        bot_profile_key: str | None = None,
        copy_mode: str = "tiktok_session",
    ) -> dict[str, Any]:
        source_root = self._google_chrome_user_data_dir()
        source_profile = (source_root / chrome_profile_id).resolve()
        if not source_root.exists():
            raise FileNotFoundError(f"Google Chrome User Data not found: {source_root}")
        try:
            source_profile.relative_to(source_root.resolve())
        except ValueError as exc:
            raise ValueError(f"Invalid Chrome profile: {chrome_profile_id}") from exc
        if not source_profile.exists():
            raise FileNotFoundError(f"Chrome profile not found: {source_profile}")
        if not (source_profile / "Preferences").exists():
            raise FileNotFoundError(f"Chrome profile does not look complete: {source_profile}")

        chrome_pids = self._running_process_pids_by_name({"chrome.exe", "chrome"})
        if chrome_pids:
            raise RuntimeError(
                "Закрой все окна Google Chrome перед импортом. "
                f"Chrome сейчас запущен, PID: {', '.join(str(pid) for pid in chrome_pids[:8])}"
            )

        worker = self.get_worker_status()
        if worker.running:
            raise RuntimeError(
                "Останови worker перед импортом профиля, иначе Chromium может держать user_data открытым. "
                f"Текущий PID worker: {worker.pid}"
            )

        profile_root = self._bot_profile_root(bot_profile_key)
        browser_dir = profile_root / "browser"
        user_data_dir = browser_dir / "user_data"
        state_dir = profile_root / "state"
        auth_backoff_path = state_dir / "auth_backoff.json"
        browser_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        temp_user_data_dir = browser_dir / f"user_data_import_tmp_{timestamp}"
        suffix = 1
        while temp_user_data_dir.exists():
            suffix += 1
            temp_user_data_dir = browser_dir / f"user_data_import_tmp_{timestamp}_{suffix}"
        temp_user_data_dir.mkdir(parents=True, exist_ok=False)

        import_details: dict[str, Any]
        try:
            mode = str(copy_mode or "tiktok_session").strip().lower()
            if mode in {"full", "full_profile", "profile"}:
                copied_root_files: list[str] = []
                for file_name in ("Local State", "Last Version", "First Run"):
                    source_file = source_root / file_name
                    if source_file.exists():
                        shutil.copy2(source_file, temp_user_data_dir / file_name)
                        copied_root_files.append(file_name)
                shutil.copytree(source_profile, temp_user_data_dir / "Default", dirs_exist_ok=True)
                import_details: dict[str, Any] = {
                    "copy_mode": "full_profile",
                    "copied_root_files": copied_root_files,
                    "cookies": {},
                }
            else:
                import_details = self._copy_tiktok_session_profile(source_root, source_profile, temp_user_data_dir)
                import_details["copy_mode"] = "tiktok_session"

            default_import_dir = temp_user_data_dir / "Default"
            for lock_path in list(temp_user_data_dir.glob("Singleton*")) + list(default_import_dir.glob("Singleton*")):
                try:
                    if lock_path.is_dir():
                        shutil.rmtree(lock_path, ignore_errors=True)
                    else:
                        lock_path.unlink(missing_ok=True)
                except Exception:
                    pass

            validation = self._validate_staged_chromium_import(
                temp_user_data_dir,
                copy_mode=str(import_details["copy_mode"]),
                import_details=import_details,
            )
            if not validation["ok"]:
                raise RuntimeError("Импорт отменён: " + " ".join(validation["errors"]))
        except Exception:
            if temp_user_data_dir.exists():
                shutil.rmtree(temp_user_data_dir, ignore_errors=True)
            raise

        pre_import_auth_backup = self.create_auth_backup(profile_root.name) if user_data_dir.exists() else None
        try:
            backup_path, rollback = self._commit_staged_browser_profile(
                browser_dir=browser_dir,
                user_data_dir=user_data_dir,
                staged_user_data_dir=temp_user_data_dir,
                timestamp=timestamp,
            )
        except Exception:
            if temp_user_data_dir.exists():
                shutil.rmtree(temp_user_data_dir, ignore_errors=True)
            raise

        return {
            "chrome_profile_id": chrome_profile_id,
            "source_profile": str(source_profile),
            "bot_profile_key": profile_root.name,
            "destination": str(user_data_dir),
            "backup": str(backup_path) if backup_path else "",
            "pre_import_auth_backup": pre_import_auth_backup,
            "rollback": rollback,
            "validation": validation,
            "size_bytes": self._dir_size_bytes(user_data_dir),
            "auth_backoff_removed": False,
            "auth_backoff_reset_error": "",
            "auth_backoff_preserved": auth_backoff_path.exists(),
            **import_details,
        }

    def begin_dedicated_login(self, bot_profile_key: str | None = None) -> dict[str, Any]:
        profile_root = self._bot_profile_root(bot_profile_key)
        browser_dir = profile_root / 'browser'
        chrome = find_google_chrome()
        with browser_lease(browser_dir):
            if self.get_worker_status().running:
                raise RuntimeError('Остановите worker перед ручным входом.')
            active = browser_dir / 'user_data'
            # Reauthentication reuses the same Chrome profile and device storage.
            # Only the first migration stages a new, independent login.
            mode = 'active' if uses_google_chrome(active) else 'staged'
            target = active if mode == 'active' else browser_dir / 'dedicated_login' / 'user_data'
            already_open = chrome_profile_running(target)
            if mode == 'active' and not already_open and not (browser_dir / LOGIN_PENDING).exists():
                self.create_auth_backup(profile_root.name)
            mark_google_chrome(target)
            (browser_dir / LOGIN_PENDING).write_text(json.dumps({'mode': mode}), encoding='utf-8')
            if not already_open:
                try:
                    subprocess.Popen(
                        [str(chrome), f'--user-data-dir={target}', '--profile-directory=Default',
                         '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                         'https://www.tiktok.com/messages'],
                        cwd=str(self.base_dir), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    raise RuntimeError('Не удалось открыть Google Chrome. Повторите вход по кнопке.') from None
            return {'bot_profile_key': profile_root.name, 'pending': True, 'already_open': already_open}

    def finish_dedicated_login(self, bot_profile_key: str | None = None) -> dict[str, Any]:
        from yara_app.auth_flow import clear_auth_backoff, record_session_validation

        profile_root = self._bot_profile_root(bot_profile_key)
        browser_dir = profile_root / 'browser'
        with browser_lease(browser_dir):
            if self.get_worker_status().running:
                raise RuntimeError('Остановите worker перед проверкой входа.')
            pending = browser_dir / LOGIN_PENDING
            data = self._read_json(pending, {})
            if data.get('mode') not in {'active', 'staged'}:
                raise RuntimeError('Сначала нажмите «Открыть Chrome для входа».')
            destination = browser_dir / 'user_data'
            target = destination if data['mode'] == 'active' else browser_dir / 'dedicated_login' / 'user_data'
            if not uses_google_chrome(target):
                raise RuntimeError('Отдельный профиль не найден. Откройте Chrome для входа заново.')
            if chrome_profile_running(target):
                raise RuntimeError('Закройте отдельное окно Chrome для входа, затем нажмите «Проверить вход». Обычные окна Chrome можно оставить.')
            verify_chrome_session(target)
            if self.get_worker_status().running:
                raise RuntimeError('Worker запущен во время проверки. Остановите его и повторите проверку.')
            previous = None
            if target != destination:
                if destination.exists():
                    self.create_auth_backup(profile_root.name)
                previous, _rollback = self._commit_staged_browser_profile(
                    browser_dir=browser_dir, user_data_dir=destination, staged_user_data_dir=target,
                    timestamp=time.strftime('%Y%m%d_%H%M%S'),
                )
            clear_auth_backoff(profile_root / 'state' / 'auth_backoff.json')
            (profile_root / 'state' / 'session_import_required.json').unlink(missing_ok=True)
            record_session_validation(profile_root / 'state', valid=True, source='dedicated_chrome')
            pending.unlink()
            return {'bot_profile_key': profile_root.name, 'verified': True, 'channel': 'chrome',
                    'backup': str(previous) if previous else ''}

    def cancel_dedicated_login(self, bot_profile_key: str | None = None) -> dict[str, Any]:
        profile_root = self._bot_profile_root(bot_profile_key)
        browser_dir = profile_root / 'browser'
        with browser_lease(browser_dir):
            pending = browser_dir / LOGIN_PENDING
            data = self._read_json(pending, {})
            target = browser_dir / 'user_data' if data.get('mode') == 'active' else browser_dir / 'dedicated_login' / 'user_data'
            if chrome_profile_running(target):
                raise RuntimeError('Сначала закройте отдельное окно Chrome для входа.')
            if data.get('mode') == 'active':
                # The active session may have changed during manual login.
                from yara_app.auth_flow import record_session_validation
                state_dir = profile_root / 'state'
                state_dir.mkdir(parents=True, exist_ok=True)
                (state_dir / 'session_import_required.json').write_text(
                    json.dumps({'reason': 'manual_login_cancelled', 'at': time.time()}), encoding='utf-8',
                )
                record_session_validation(state_dir, valid=None, source='manual_login_cancelled')
            pending.unlink(missing_ok=True)
            return {'bot_profile_key': profile_root.name, 'cancelled': True}

    def import_chrome_request(self, *, request_text: str, bot_profile_key: str | None = None) -> dict[str, Any]:
        with browser_lease(self._bot_profile_root(bot_profile_key) / 'browser'):
            return self._import_chrome_request(request_text=request_text, bot_profile_key=bot_profile_key)

    def _import_chrome_request(self, *, request_text: str, bot_profile_key: str | None = None) -> dict[str, Any]:
        from yara_app.auth_flow import clear_auth_backoff, record_session_validation
        from yara_app.session_import import cookies_from_curl, prepare_session

        cookies = cookies_from_curl(request_text)
        profile_root = self._bot_profile_root(bot_profile_key)
        if self.get_worker_status().running:
            raise RuntimeError('Остановите worker перед переносом авторизации, затем нажмите кнопку ещё раз.')
        browser_dir = profile_root / 'browser'
        if (browser_dir / LOGIN_PENDING).exists():
            raise RuntimeError('Сначала завершите ручной вход через отдельный Chrome.')
        browser_dir.mkdir(parents=True, exist_ok=True)
        lock = browser_dir / '.session_import.lock'
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError('Импорт уже выполняется. Дождитесь результата.') from None
        try:
            with tempfile.TemporaryDirectory(prefix='session_import_', dir=browser_dir) as temp_dir:
                staged = Path(temp_dir) / 'user_data'
                prepare_session(staged, cookies)
                if self.get_worker_status().running:
                    raise RuntimeError('Worker был запущен во время проверки. Остановите его и повторите импорт.')
                destination = browser_dir / 'user_data'
                backup = self.create_auth_backup(profile_root.name) if destination.exists() else None
                previous, _rollback = self._commit_staged_browser_profile(
                    browser_dir=browser_dir, user_data_dir=destination, staged_user_data_dir=staged,
                    timestamp=time.strftime('%Y%m%d_%H%M%S'),
                )
                clear_auth_backoff(profile_root / 'state' / 'auth_backoff.json')
                (profile_root / 'state' / 'session_import_required.json').unlink(missing_ok=True)
                record_session_validation(profile_root / 'state', valid=True, source='import')
                return {
                    'bot_profile_key': profile_root.name, 'verified': True, 'cookies_count': len(cookies),
                    'backup': str(previous) if previous else '', 'pre_import_auth_backup': backup,
                }
        finally:
            os.close(lock_fd)
            lock.unlink(missing_ok=True)

    def _remove_profile_path_for_compact(self, path: Path, allowed_root: Path) -> int:
        if not path.exists():
            return 0
        resolved_path = path.resolve()
        resolved_root = allowed_root.resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"Refusing to remove path outside browser profile: {resolved_path}") from exc
        size = self._dir_size_bytes(path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return size

    def compact_browser_profile(
        self, profile_key: str | None = None, *, filter_cookies: bool = True,
    ) -> dict[str, Any]:
        profile_root = self._bot_profile_root(profile_key)
        with browser_lease(profile_root / 'browser'):
            if (profile_root / 'browser' / LOGIN_PENDING).exists() or chrome_profile_running(profile_root / 'browser' / 'user_data'):
                raise RuntimeError('Завершите ручной вход и закройте отдельный Chrome перед очисткой кэша.')
            return self._compact_browser_profile(profile_key, filter_cookies=filter_cookies)

    def _compact_browser_profile(
        self,
        profile_key: str | None = None,
        *,
        filter_cookies: bool = True,
    ) -> dict[str, Any]:
        worker = self.get_worker_status()
        if worker.running:
            raise RuntimeError(
                "Останови worker перед очисткой Chromium-профиля. "
                f"Текущий PID worker: {worker.pid}"
            )

        profile_root = self._bot_profile_root(profile_key)
        user_data_dir = profile_root / "browser" / "user_data"
        default_dir = user_data_dir / "Default"
        if not default_dir.exists():
            raise FileNotFoundError(f"Chromium profile not found: {default_dir}")

        removed: list[dict[str, Any]] = []
        freed_bytes = 0
        for base_dir in (default_dir, user_data_dir):
            for name in CHROMIUM_COMPACT_DIRS:
                # Site/service-worker storage may hold device and session state.
                if name == 'Service Worker':
                    continue
                target = base_dir / name
                size = self._remove_profile_path_for_compact(target, user_data_dir)
                if size:
                    freed_bytes += size
                    removed.append({"path": str(target), "bytes": size, "type": "dir"})
            for name in CHROMIUM_COMPACT_FILES:
                target = base_dir / name
                size = self._remove_profile_path_for_compact(target, user_data_dir)
                if size:
                    freed_bytes += size
                    removed.append({"path": str(target), "bytes": size, "type": "file"})

        indexeddb_dir = default_dir / "IndexedDB"
        dedicated = uses_google_chrome(user_data_dir)
        if indexeddb_dir.exists() and not dedicated:
            for item in indexeddb_dir.iterdir():
                if self._is_tiktok_storage_name(item.name):
                    continue
                size = self._remove_profile_path_for_compact(item, user_data_dir)
                if size:
                    freed_bytes += size
                    removed.append({"path": str(item), "bytes": size, "type": "indexeddb"})

        cookie_results: list[dict[str, Any]] = []
        if filter_cookies and not dedicated:
            for cookies_path in (default_dir / "Network" / "Cookies", default_dir / "Cookies"):
                if not cookies_path.exists():
                    continue
                result = self._filter_tiktok_cookie_db(cookies_path)
                result["copied"] = True
                cookie_results.append(result)

        return {
            "profile_key": profile_root.name,
            "user_data_dir": str(user_data_dir),
            "removed_count": len(removed),
            "removed": removed,
            "freed_bytes": freed_bytes,
            "size_bytes": self._dir_size_bytes(user_data_dir),
            "cookies": cookie_results,
        }

    def _browser_backup_dirs(self, browser_dir: Path) -> list[Path]:
        if not browser_dir.exists():
            return []
        return [
            path for path in browser_dir.iterdir()
            if path.is_dir() and (
                path.name.startswith("user_data_backup_")
                or path.name.startswith("user_data_before_chrome_import_")
            )
        ]

    def _auth_backup_files(self, profile_key: str) -> list[Path]:
        return self.backups.auth_backup_files(profile_key)

    def _auth_backup_by_name(self, profile_key: str, name: str) -> Path | None:
        return self.backups.auth_backup_by_name(profile_key, name)

    @staticmethod
    def _safe_zip_name(name: str) -> bool:
        return BackupService.safe_zip_name(name)

    def _auth_restore_policy(self, profile_key: str, *, include_global: bool = False) -> dict[str, tuple[str, ...]]:
        return self.backups.auth_restore_policy(profile_key, include_global=include_global)

    def _classify_auth_backup_member(
        self,
        member_name: str,
        profile_key: str,
        *,
        include_global: bool = False,
    ) -> str:
        return self.backups.classify_auth_backup_member(
            member_name,
            profile_key,
            include_global=include_global,
        )

    def validate_auth_backup(
        self,
        profile_key: str | None,
        backup_name: str,
        *,
        include_global: bool = False,
        deep: bool = False,
    ) -> dict[str, Any]:
        return self.backups.validate_auth_backup(
            profile_key,
            backup_name,
            include_global=include_global,
            deep=deep,
        )

    def restore_auth_backup(
        self,
        profile_key: str | None,
        backup_name: str,
        *,
        include_global: bool = False,
    ) -> dict[str, Any]:
        profile_root = self._bot_profile_root(profile_key)
        with browser_lease(profile_root / 'browser'):
            if (profile_root / 'browser' / LOGIN_PENDING).exists() or chrome_profile_running(profile_root / 'browser' / 'user_data'):
                raise RuntimeError('Завершите ручной вход и закройте отдельный Chrome перед восстановлением.')
            return self.backups.restore_auth_backup(
                profile_key, backup_name, include_global=include_global,
            )

    def prune_auth_backups(
        self,
        profile_key: str | None = None,
        *,
        keep_latest: int = 1,
    ) -> dict[str, Any]:
        return self.backups.prune_auth_backups(profile_key, keep_latest=keep_latest)

    def delete_selected_backups(self, profile_key: str | None, selections: list[dict[str, Any]]) -> dict[str, Any]:
        return self.backups.delete_selected_backups(profile_key, selections)

    def prune_browser_backups(
        self,
        profile_key: str | None = None,
        *,
        keep_latest: int = 1,
    ) -> dict[str, Any]:
        return self.backups.prune_browser_backups(profile_key, keep_latest=keep_latest)

    def _auth_backoff_seconds_left(self, path: Path) -> int:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            until_ts = float(payload.get("until", 0))
        except Exception:
            return 0
        return max(0, int(until_ts - time.time()))

    def _session_validation_summary(self, profile_root: Path) -> dict[str, Any]:
        payload = self._read_json(profile_root / 'state' / 'session_validation.json', {})
        if not isinstance(payload, dict) or type(payload.get('valid')) is not bool:
            return {'valid': None, 'checked_at': None}
        checked_at = payload.get('checked_at')
        if not isinstance(checked_at, (int, float)) or not 0 < checked_at <= time.time() + 60:
            return {'valid': None, 'checked_at': None}
        return {'valid': payload['valid'], 'checked_at': checked_at}

    def browser_profile_summary(self, profile_key: str | None = None) -> dict[str, Any]:
        profile_root = self._bot_profile_root(profile_key)
        browser_dir = profile_root / "browser"
        user_data_dir = browser_dir / "user_data"
        default_dir = user_data_dir / "Default"
        local_state_path = user_data_dir / "Local State"
        preferences_path = default_dir / "Preferences"
        cookies_candidates = [
            default_dir / "Network" / "Cookies",
            default_dir / "Cookies",
        ]
        cookies_path = next((path for path in cookies_candidates if path.exists()), cookies_candidates[0])
        auth_backoff_path = profile_root / "state" / "auth_backoff.json"

        backups: list[dict[str, Any]] = []
        backup_dirs: list[Path] = []
        if browser_dir.exists():
            backup_dirs = self._browser_backup_dirs(browser_dir)
            for backup in sorted(backup_dirs, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)[:5]:
                try:
                    modified_at = backup.stat().st_mtime
                except OSError:
                    modified_at = None
                backups.append({
                    "name": backup.name,
                    "path": str(backup),
                    "modified_at": modified_at,
                    "size_bytes": self._dir_size_bytes(backup),
                    "kind": "browser",
                })

        auth_backups: list[dict[str, Any]] = []
        auth_backup_files = self._auth_backup_files(profile_root.name)
        for backup in sorted(auth_backup_files, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)[:5]:
            try:
                modified_at = backup.stat().st_mtime
                size_bytes = backup.stat().st_size
            except OSError:
                modified_at = None
                size_bytes = 0
            validation = self.validate_auth_backup(profile_root.name, backup.name, deep=False)
            auth_backups.append({
                "name": backup.name,
                "path": str(backup),
                "modified_at": modified_at,
                "size_bytes": size_bytes,
                "kind": "auth",
                "valid": bool(validation.get("ok")),
                "entry_count": int(validation.get("entry_count") or 0),
                "blocked_count": int(validation.get("blocked_count") or 0),
                "manifest_profile": str(validation.get("manifest_profile") or ""),
            })

        user_data_stat = None
        try:
            user_data_stat = user_data_dir.stat() if user_data_dir.exists() else None
        except OSError:
            user_data_stat = None

        auth_backoff_left = self._auth_backoff_seconds_left(auth_backoff_path) if auth_backoff_path.exists() else 0
        try:
            needs_recovery = browser_profile_needs_recovery(user_data_dir) if user_data_dir.exists() else False
        except Exception:
            needs_recovery = False

        return {
            "profile_key": profile_root.name,
            "profile_root": str(profile_root),
            "browser_dir": str(browser_dir),
            "user_data_dir": str(user_data_dir),
            "exists": user_data_dir.exists(),
            "size_bytes": self._dir_size_bytes(user_data_dir),
            "backup_bytes": sum(self._dir_size_bytes(path) for path in backup_dirs),
            "auth_backup_bytes": sum(path.stat().st_size for path in auth_backup_files if path.exists()),
            "browser_total_bytes": self._dir_size_bytes(browser_dir),
            "default_profile_exists": default_dir.exists(),
            "local_state_exists": local_state_path.exists(),
            "preferences_exists": preferences_path.exists(),
            "cookies_exists": cookies_path.exists(),
            "cookies_path": str(cookies_path),
            "modified_at": user_data_stat.st_mtime if user_data_stat else None,
            "needs_recovery": needs_recovery,
            "session_import_required": (profile_root / 'state' / 'session_import_required.json').exists(),
            "manual_login_pending": (browser_dir / LOGIN_PENDING).exists(),
            "browser_channel": 'chrome' if uses_google_chrome(user_data_dir) else 'chromium',
            "session_validation": self._session_validation_summary(profile_root),
            "auth_backoff_exists": auth_backoff_path.exists(),
            "auth_backoff_left": auth_backoff_left,
            "auth_backoff_path": str(auth_backoff_path),
            "backup_count": len(backup_dirs),
            "auth_backup_count": len(auth_backup_files),
            "latest_backups": backups,
            "latest_auth_backups": auth_backups,
        }

    def browser_profile_live_status(self, profile_key: str | None = None) -> dict[str, Any]:
        profile_root = self._bot_profile_root(profile_key)
        browser_dir = profile_root / "browser"
        user_data_dir = browser_dir / "user_data"
        default_dir = user_data_dir / "Default"
        local_state_path = user_data_dir / "Local State"
        preferences_path = default_dir / "Preferences"
        cookies_candidates = [
            default_dir / "Network" / "Cookies",
            default_dir / "Cookies",
        ]
        cookies_path = next((path for path in cookies_candidates if path.exists()), cookies_candidates[0])
        auth_backoff_path = profile_root / "state" / "auth_backoff.json"
        try:
            needs_recovery = browser_profile_needs_recovery(user_data_dir) if user_data_dir.exists() else False
        except Exception:
            needs_recovery = False
        return {
            "profile_key": profile_root.name,
            "browser_dir": str(browser_dir),
            "user_data_dir": str(user_data_dir),
            "exists": user_data_dir.exists(),
            "default_profile_exists": default_dir.exists(),
            "local_state_exists": local_state_path.exists(),
            "preferences_exists": preferences_path.exists(),
            "cookies_exists": cookies_path.exists(),
            "cookies_path": str(cookies_path),
            "needs_recovery": needs_recovery,
            "session_import_required": (profile_root / 'state' / 'session_import_required.json').exists(),
            "manual_login_pending": (browser_dir / LOGIN_PENDING).exists(),
            "browser_channel": 'chrome' if uses_google_chrome(user_data_dir) else 'chromium',
            "session_validation": self._session_validation_summary(profile_root),
            "auth_backoff_exists": auth_backoff_path.exists(),
            "auth_backoff_left": self._auth_backoff_seconds_left(auth_backoff_path) if auth_backoff_path.exists() else 0,
            "backup_count": len(self._browser_backup_dirs(browser_dir)),
            "auth_backup_count": len(self._auth_backup_files(profile_root.name)),
        }

    def queue_snapshot(self, profile_key: str | None = None) -> dict[str, Any]:
        profiles = self.get_profiles()
        selected_key = str(profile_key or self._active_profile_name() or "")
        profile = next((item for item in profiles if item.key == selected_key), None)
        if profile is None:
            profile = next((item for item in profiles if item.active), None)
        if profile is None and profiles:
            profile = profiles[0]
        if profile is None:
            return {
                "profile_key": selected_key or "default",
                "target_count": 0,
                "ready_count": 0,
                "cooldown_count": 0,
                "next_target": "",
                "next_send_at": None,
                "next_send_at_text": "",
                "next_send_in_s": 0,
                "next_send_in_text": "нет адресатов",
            }

        state = self.get_control_state()
        cooldown_h = float(state.get("cooldown_hours") or self.DEFAULT_COOLDOWN_HOURS)
        targets = list(profile.raw.get("targets") or [])
        ready_count = 0
        cooldown_count = 0
        next_item: dict[str, Any] | None = None
        for target in targets:
            name = str(target.get("name") or target.get("url") or "")
            target_state = self.get_target_state(name, profile.key, cooldown_hours=cooldown_h)
            if target_state.get("ready"):
                ready_count += 1
                continue
            if target_state.get('delivery_unconfirmed'):
                continue
            cooldown_count += 1
            next_send_at = target_state.get("next_send_at")
            if next_send_at and (next_item is None or float(next_send_at) < float(next_item.get("next_send_at") or 0)):
                next_item = {
                    "target": name,
                    "next_send_at": next_send_at,
                    "next_send_at_text": target_state.get("next_send_at_text") or "",
                    "next_send_in_s": int(target_state.get("cooldown_left_s") or 0),
                    "next_send_in_text": target_state.get("cooldown_left_text") or "",
                }

        return {
            "profile_key": profile.key,
            "target_count": len(targets),
            "ready_count": ready_count,
            "cooldown_count": cooldown_count,
            "next_target": str((next_item or {}).get("target") or ""),
            "next_send_at": (next_item or {}).get("next_send_at"),
            "next_send_at_text": str((next_item or {}).get("next_send_at_text") or ""),
            "next_send_in_s": int((next_item or {}).get("next_send_in_s") or 0),
            "next_send_in_text": str((next_item or {}).get("next_send_in_text") or ("сейчас" if ready_count else "нет данных")),
        }

    # ── Target state helpers ──────────────────────────────────────────────────

    def _get_profile_state_dir(self, profile_key: str | None = None) -> Path:
        """Return the state dir for a profile (profiles/<key>/state/)."""
        if profile_key is None:
            state = self.get_control_state()
            profile_key = str(state.get("active_profile") or "default")
        return self.base_dir / "profiles" / profile_key / "state"

    @staticmethod
    def _make_file_keys(target_name: str) -> list[str]:
        """Return all known filename keys for a target.

        The runtime persists files through :mod:`ttbot.names`, so UI tools must
        derive keys from the same helpers. We still keep a fallback to the older
        pre-refactor key builder in case a project already contains files that
        were created by earlier UI-only builds.
        """
        import hashlib
        import re
        import unicodedata

        raw = target_name or ''
        keys: list[str] = [safe_name_key(raw), legacy_safe_filename(raw)]

        # Backward-compatible fallback for older UI-only builds that used a
        # simplified NFKC-only hash and slug computation.
        normalized = unicodedata.normalize('NFKC', raw)
        ascii_slug = unicodedata.normalize('NFKD', normalized).encode('ascii', 'ignore').decode('ascii')
        ascii_slug = re.sub(r'[\W_]+', '_', ascii_slug).strip('_').lower()
        digest = hashlib.sha1((normalized or 'target').encode('utf-8')).hexdigest()[:10]
        prefix = ascii_slug[:40] if ascii_slug else 'target'
        keys.append(f'{prefix}_{digest}')

        return list(dict.fromkeys(k for k in keys if k))

    def _iter_target_state_paths(self, target_name: str, profile_key: str | None = None) -> dict[str, list[Path]]:
        """Return candidate state files for the target.

        Keys are derived exactly like the runtime does so UI actions affect the
        same files that ``StateStore`` reads and writes.
        """
        state_dir = self._get_profile_state_dir(profile_key)
        keys = self._make_file_keys(target_name)
        return {
            'last_send': [state_dir / f'last_send_{key}.txt' for key in keys],
            'stats': [state_dir / f'stats_{key}.txt' for key in keys],
        }

    def get_target_state(
        self,
        target_name: str,
        profile_key: str | None = None,
        *,
        cooldown_hours: float | None = None,
    ) -> dict[str, Any]:
        """Read runtime state for a target using the same file rules as worker."""
        import time

        paths = self._iter_target_state_paths(target_name, profile_key)

        last_send_at: float | None = None
        streak_count: int = 0
        streak_date: str = ''

        for path in paths['last_send']:
            if path.exists():
                try:
                    last_send_at = float(path.read_text(encoding='utf-8').strip())
                    break
                except Exception:
                    continue

        for path in paths['stats']:
            if path.exists():
                try:
                    parts = path.read_text(encoding='utf-8').strip().split('|')
                    streak_count = int(parts[0]) if parts and parts[0] else 0
                    streak_date = parts[1] if len(parts) > 1 else ''
                    break
                except Exception:
                    continue

        cooldown_h = float(cooldown_hours or 0) if cooldown_hours is not None else 0.0
        if cooldown_h <= 0:
            state = self.get_control_state()
            cooldown_h = float(state.get('cooldown_hours') or 12)
        cooldown_total_s = max(1, int(cooldown_h * 3600))
        cooldown_left_h: float = 0.0
        cooldown_left_s: int = 0
        next_send_at: float | None = None
        if last_send_at is not None:
            passed = max(0.0, time.time() - last_send_at)
            left_s = max(0.0, cooldown_total_s - passed)
            cooldown_left_h = left_s / 3600
            cooldown_left_s = int(left_s)
            next_send_at = last_send_at + cooldown_total_s

        existing_last_send = [str(path) for path in paths['last_send'] if path.exists()]
        existing_stats = [str(path) for path in paths['stats'] if path.exists()]
        cooldown_progress = 100.0 if cooldown_left_s <= 0 else 100.0 - ((cooldown_left_s / cooldown_total_s) * 100.0)
        delivery_guard = self._read_json(
            self._get_profile_state_dir(profile_key) / 'target_send_guards' / f'{safe_name_key(target_name)}.json', {},
        )
        delivery_unconfirmed = isinstance(delivery_guard, dict) and delivery_guard.get('status') == 'unconfirmed'

        return {
            'last_send_at': last_send_at,
            'streak_count': streak_count,
            'streak_date': streak_date,
            'cooldown_left_h': round(cooldown_left_h, 2),
            'cooldown_left_s': cooldown_left_s,
            'cooldown_total_s': cooldown_total_s,
            'cooldown_progress_percent': round(max(0.0, min(100.0, cooldown_progress)), 1),
            'cooldown_left_text': self._format_seconds_short(cooldown_left_s),
            'next_send_at': next_send_at,
            'next_send_at_text': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_send_at)) if next_send_at else '',
            'ready': cooldown_left_s <= 0 and not delivery_unconfirmed,
            'delivery_unconfirmed': delivery_unconfirmed,
            'state_files': {
                'last_send': existing_last_send,
                'stats': existing_stats,
            },
        }

    @staticmethod
    def _format_seconds_short(seconds: int) -> str:
        seconds = max(0, int(seconds))
        if seconds <= 0:
            return 'сейчас'
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours and minutes:
            return f'{hours} ч {minutes} мин'
        if hours:
            return f'{hours} ч'
        if minutes:
            return f'{minutes} мин'
        return 'меньше минуты'

    def reset_target_cooldown(self, target_name: str, profile_key: str | None = None) -> tuple[int, str]:
        """Delete the active last_send file for a target.

        Finds the first existing last_send file (the one the runtime would read),
        deletes only that file, and returns ``(1, path_str)`` on success or
        ``(0, '')`` if no file was found.
        """
        guard_path = self._get_profile_state_dir(profile_key) / 'target_send_guards' / f'{safe_name_key(target_name)}.json'
        guard = self._read_json(guard_path, {})
        cleared_guard = isinstance(guard, dict) and guard.get('status') == 'unconfirmed'
        if cleared_guard:
            guard_path.unlink(missing_ok=True)
        for path in self._iter_target_state_paths(target_name, profile_key)['last_send']:
            if path.exists():
                try:
                    path.unlink()
                except FileNotFoundError:
                    return 0, ''
                return 1, str(path)
        return (1, str(guard_path)) if cleared_guard else (0, '')

    def set_target_streak(self, target_name: str, new_count: int,
                          profile_key: str | None = None) -> tuple[int, str]:
        """Write streak count to the active stats file.

        Finds the first existing stats file and updates only that file,
        preserving the current date marker. If no stats file exists yet, a new
        file is created using the primary stable key.
        """
        import datetime

        state_dir = self._get_profile_state_dir(profile_key)
        state_dir.mkdir(parents=True, exist_ok=True)
        stats_paths = self._iter_target_state_paths(target_name, profile_key)['stats']

        today = datetime.date.today().strftime('%Y-%m-%d')
        existing_date = today
        target_path: Path | None = None

        for path in stats_paths:
            if path.exists():
                try:
                    parts = path.read_text(encoding='utf-8').strip().split('|')
                    if len(parts) > 1 and parts[1]:
                        existing_date = parts[1]
                except Exception:
                    pass
                target_path = path
                break

        if target_path is None:
            target_path = stats_paths[0]

        payload = f'{int(new_count)}|{existing_date}'
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(payload, encoding='utf-8')
        return 1, str(target_path)

    def toggle_profile(self, index: int) -> ProfileEntry:
        payload = self._read_json(self.profiles_path, {})
        if isinstance(payload, list):
            if index < 0 or index >= len(payload) or not isinstance(payload[index], dict):
                raise IndexError("profile index out of range")
            current = bool(payload[index].get("enabled", True))
            payload[index]["enabled"] = not current
            self._write_json(self.profiles_path, payload)
            return self.get_profiles()[index]
        if isinstance(payload, dict):
            profiles = self.get_profiles()
            if index < 0 or index >= len(profiles):
                raise IndexError("profile index out of range")
            profile = profiles[index]
            raw = payload.get(profile.key)
            if not isinstance(raw, dict) or "enabled" not in raw:
                raise ValueError("This profile format does not support enable/disable")
            raw["enabled"] = not bool(raw.get("enabled"))
            payload[profile.key] = raw
            self._write_json(self.profiles_path, payload)
            return next(item for item in self.get_profiles() if item.key == profile.key)
        raise ValueError("Profiles configuration must be a list or object")

    def toggle_profile_key(self, profile_key: str) -> ProfileEntry:
        key = str(profile_key or "").strip()
        profiles = self.get_profiles()
        for index, profile in enumerate(profiles):
            if profile.key == key:
                return self.toggle_profile(index)
        raise ValueError(f"Unknown profile: {key}")

    def _message_pool_stats_from_text(self, raw: str, *, exists: bool = True) -> dict[str, Any]:
        all_lines = str(raw or "").splitlines()
        raw_lines = len(all_lines)
        blank_lines = sum(1 for ln in all_lines if not ln.strip())
        comment_lines = sum(1 for ln in all_lines if ln.strip().startswith("#"))
        messages = [ln.strip() for ln in all_lines if ln.strip() and not ln.strip().startswith("#")]
        seen: set[str] = set()
        unique: list[str] = []
        for item in messages:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        lengths = [len(m) for m in unique]
        return {
            "exists": exists,
            "count": len(unique),
            "unique_count": len(unique),
            "raw_lines": raw_lines,
            "usable_count": len(messages),
            "duplicates": len(messages) - len(unique),
            "duplicate_count": len(messages) - len(unique),
            "blank_lines": blank_lines,
            "comment_lines": comment_lines,
            "max_length": max(lengths) if lengths else 0,
            "avg_length": round(sum(lengths) / len(lengths)) if lengths else 0,
            "sample": unique[:8],
        }

    def get_message_pool_stats(self) -> dict[str, Any]:
        if not self.message_pool_path.exists():
            return self._message_pool_stats_from_text("", exists=False)
        raw = self.message_pool_path.read_text(encoding="utf-8", errors="replace")
        return self._message_pool_stats_from_text(raw, exists=True)

    def normalize_message_pool_text(self, text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n")
        lines = normalized.split("\n")
        cleaned: list[str] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                cleaned.append(stripped)
                continue
            if stripped not in seen:
                seen.add(stripped)
                cleaned.append(stripped)
        return "\n".join(cleaned) + ("\n" if cleaned else "")

    def get_message_pool_text(self) -> str:
        if not self.message_pool_path.exists():
            return self.DEFAULT_MESSAGE_POOL
        return self.message_pool_path.read_text(encoding="utf-8", errors="replace")

    def save_message_pool_text_raw(self, text: str) -> dict[str, Any]:
        payload = str(text or "").replace("\r\n", "\n")
        if payload and not payload.endswith("\n"):
            payload += "\n"
        self.message_pool_path.write_text(payload, encoding="utf-8")
        return self.get_message_pool_stats()

    def create_message_pool_backup(self, text: str | None = None) -> Path:
        backup_dir = self.base_dir / "backups" / "message_pool"
        backup_dir.mkdir(parents=True, exist_ok=True)
        payload = self.get_message_pool_text() if text is None else str(text)
        payload = payload.replace("\r\n", "\n")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"message_pool_{stamp}.txt"
        counter = 1
        while target.exists():
            target = backup_dir / f"message_pool_{stamp}_{counter}.txt"
            counter += 1
        target.write_text(payload, encoding="utf-8")
        return target

    def create_public_project_backup(self) -> dict[str, Any]:
        return self.backups.create_public_project_backup()

    def create_auth_backup(self, profile_key: str | None = None) -> dict[str, Any]:
        return self.backups.create_auth_backup(profile_key)
    def save_message_pool_text(self, text: str) -> dict[str, Any]:
        normalized = self.normalize_message_pool_text(text)
        self.message_pool_path.write_text(normalized, encoding="utf-8")
        return self.get_message_pool_stats()

    def _active_profile_artifacts_dir(self) -> Path:
        return self.base_dir / "profiles" / self._active_profile_name() / "artifacts"

    def get_current_run_snapshot(self) -> dict[str, Any]:
        artifacts_dir = self._active_profile_artifacts_dir()
        run_state_path = artifacts_dir / "run_state.json"
        run_summary_path = artifacts_dir / "run_summary.json"

        run_state = self._read_json(run_state_path, {})
        run_summary = self._read_json(run_summary_path, {})
        if not isinstance(run_state, dict):
            run_state = {}
        if not isinstance(run_summary, dict):
            run_summary = {}

        inline_summary = run_state.get("summary") if isinstance(run_state.get("summary"), dict) else {}
        summary = dict(inline_summary)
        summary.update(run_summary)
        results = list(summary.get("results") or [])
        success_count = int(summary.get("success_count") or 0)
        skipped_count = int(summary.get("skipped_count") or 0)
        failed_count = int(summary.get("failed_count") or 0)
        status = str(run_state.get("status") or ("idle" if summary else "unknown"))
        if status == "idle" and summary:
            if failed_count:
                status = "partial" if success_count else "failed"
            elif success_count:
                status = "success"
            elif skipped_count:
                status = "skipped"
        updated_at = float(run_state.get("completed_at") or run_state.get("timestamp") or 0)

        return {
            "profile_name": self._active_profile_name(),
            "artifacts_dir": str(artifacts_dir),
            "run_state_path": str(run_state_path),
            "run_summary_path": str(run_summary_path),
            "state_exists": run_state_path.exists(),
            "summary_exists": run_summary_path.exists(),
            "status": status,
            "reason": str(run_state.get("reason") or ""),
            "updated_at": updated_at or None,
            "completed_at": float(run_state.get("completed_at") or 0) or None,
            "is_active": status in {"starting", "running"},
            "current_target": run_state.get("current_target"),
            "total_targets": int(run_state.get("total_targets") or summary.get("total_targets") or 0),
            "success_count": success_count,
            "delivery_verified": success_count > 0 and sum(
                1 for result in results if isinstance(result, dict) and result.get('success')
                and result.get('confirmation') == 'server_ack'
            ) == success_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "duration_seconds": float(summary.get("duration_seconds") or 0),
            "results_count": len(results),
            "summary": summary,
        }

    def get_run_history(self, profile_key: str | None = None, *, limit: int = 40) -> list[dict[str, Any]]:
        profile = str(profile_key or self._active_profile_name() or "default")
        history_path = self.base_dir / "profiles" / profile / "artifacts" / "run_history.jsonl"
        limit = max(1, min(300, int(limit)))
        if not history_path.exists():
            return []
        items: list[dict[str, Any]] = []
        try:
            file_size = history_path.stat().st_size
            with history_path.open("rb") as handle:
                start = max(0, file_size - RUN_HISTORY_READ_BYTES)
                handle.seek(start)
                raw = handle.read()
        except OSError:
            return []
        if start:
            separator = raw.find(b"\n")
            raw = raw[separator + 1:] if separator >= 0 else b""
        for line in raw.decode("utf-8", errors="replace").splitlines()[-limit:]:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                payload.setdefault("profile_name", profile)
                items.append(payload)
        return items

    def worker_lifecycle_summary(self, profile_key: str | None = None, *, limit: int = 12) -> dict[str, Any]:
        events = [
            item for item in self.get_run_history(profile_key, limit=max(limit * 4, 40))
            if item.get("event") in {"lifecycle", "phase_timing", "run_started", "run_finished", "run_failed"}
        ]
        recent = events[-limit:]
        last = recent[-1] if recent else {}
        phase_events = [item for item in recent if item.get("event") == "phase_timing"]
        slow = [
            item for item in phase_events
            if float(item.get("elapsed_ms") or 0) >= 5000
        ]
        return {
            "profile_key": str(profile_key or self._active_profile_name() or "default"),
            "last": last,
            "events": recent,
            "event_count": len(recent),
            "slow_count": len(slow),
            "last_stage": str(last.get("stage") or last.get("phase") or last.get("event") or ""),
        }

    def set_paused(self, value: bool) -> dict[str, Any]:
        return self.update_control_state({"paused": bool(value)})

    def set_dry_run(self, value: bool) -> dict[str, Any]:
        return self.update_control_state({"dry_run": bool(value)})

    def _decode_process_output(self, data: bytes) -> str:
        for enc in ("utf-8", "cp866", "cp1251", sys.getdefaultencoding()):
            try:
                return data.decode(enc)
            except Exception:
                continue
        return data.decode("utf-8", errors="replace")

    def _windows_subprocess_kwargs(self, *, hide_window: bool = True) -> dict[str, Any]:
        if os.name != "nt":
            return {}
        kwargs: dict[str, Any] = {}
        startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
        startf_use_showwindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if startupinfo_factory is not None:
            startupinfo = startupinfo_factory()
            startupinfo.dwFlags |= startf_use_showwindow
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
        if hide_window:
            kwargs["creationflags"] = self._windows_creation_flags(hide_window=True)
        return kwargs

    def _run_powershell_script(self, script_path: Path, args: list[str] | None = None) -> subprocess.CompletedProcess:
        if os.name != "nt":
            raise RuntimeError("Планировщик worker поддерживается только в Windows.")
        if not script_path.exists():
            raise FileNotFoundError(f"PowerShell script not found: {script_path}")
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            *(args or []),
        ]
        result = subprocess.run(
            cmd,
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **self._windows_subprocess_kwargs(hide_window=True),
        )
        if result.returncode != 0:
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
            raise RuntimeError(output or f"PowerShell exited with code {result.returncode}")
        return result

    def _run_powershell_command(self, command: str, args: list[str] | None = None) -> subprocess.CompletedProcess:
        if os.name != "nt":
            raise RuntimeError("PowerShell command is available only on Windows.")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command, *(args or [])],
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **self._windows_subprocess_kwargs(hide_window=True),
        )
        if result.returncode != 0:
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
            raise RuntimeError(output or f"PowerShell exited with code {result.returncode}")
        return result

    def register_worker_schedule(
        self,
        *,
        at_startup: bool = True,
        at_logon: bool = False,
        every_12_hours: bool = True,
        interval_hours: int = 12,
        first_run_at: str | None = None,
    ) -> dict[str, Any]:
        return self.scheduler.register(
            at_startup=at_startup,
            at_logon=at_logon,
            every_12_hours=every_12_hours,
            interval_hours=interval_hours,
            first_run_at=first_run_at,
        )

    def unregister_worker_schedule(self) -> dict[str, Any]:
        return self.scheduler.unregister()

    def get_worker_schedule_status_cached(self, *, max_age_seconds: float = 60.0) -> dict[str, Any]:
        return self.scheduler.status_cached(max_age_seconds=max_age_seconds)

    @staticmethod
    def _parse_schedule_timestamp(value: Any) -> float | None:
        return WorkerScheduleService.parse_timestamp(value)

    def _normalize_worker_schedule_status(self, status: dict[str, Any]) -> dict[str, Any]:
        return self.scheduler.normalize_status(status)

    def _get_worker_schedule_status_schtasks(self) -> dict[str, Any] | None:
        return self.scheduler._status_from_schtasks()

    def get_worker_schedule_status(self) -> dict[str, Any]:
        return self.scheduler.status()

    def _resolve_python_executable(self, *, prefer_windowless: bool = True) -> str:
        executable = sys.executable
        if os.name != "nt" or not prefer_windowless:
            return executable
        if executable.lower().endswith("pythonw.exe"):
            return executable
        candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
        return executable

    def _read_pid_file(self, path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            payload = path.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        if not payload:
            return None
        try:
            data = json.loads(payload)
            pid = data.get("pid") if isinstance(data, dict) else payload
        except Exception:
            pid = payload
        try:
            value = int(pid)
        except Exception:
            return None
        return value if value > 0 else None

    def _write_pid_file(self, path: Path, pid: int) -> None:
        if not isinstance(pid, int) or pid <= 0:
            return
        payload = {"pid": pid, "updated_at": time.time()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _clear_pid_file(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except Exception:
            return

    def _iter_python_script_pids(self, script_path: Path) -> list[int]:
        if psutil is None:
            return []
        matches: list[int] = []
        script_name = script_path.name.lower()
        script_resolved = script_path.resolve()
        script_full = str(script_resolved).lower()
        base_resolved = self.base_dir.resolve()
        module_name = ""
        try:
            relative = script_resolved.relative_to(base_resolved)
            if relative.suffix == ".py":
                module_name = ".".join(relative.with_suffix("").parts).lower()
        except Exception:
            module_name = ""
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                cmdline = proc.info.get("cmdline") or []
            except Exception:
                continue
            lowered = [str(part).lower() for part in cmdline]
            if not lowered:
                continue
            try:
                proc_cwd = Path(str(proc.info.get("cwd") or "")).resolve()
            except Exception:
                proc_cwd = None
            same_root = proc_cwd == base_resolved
            script_match = False
            for part in cmdline:
                raw_part = str(part)
                low_part = raw_part.lower()
                if low_part == script_full:
                    script_match = True
                    break
                candidate = Path(raw_part)
                if not candidate.is_absolute() and same_root:
                    candidate = base_resolved / candidate
                try:
                    if candidate.resolve() == script_resolved:
                        script_match = True
                        break
                except Exception:
                    pass
                if same_root and low_part.endswith(script_name):
                    script_match = True
                    break
            module_match = bool(module_name and same_root and module_name in lowered)
            if module_match or script_match:
                pid = int(proc.info.get("pid") or 0)
                if pid > 0:
                    matches.append(pid)
        return sorted(set(matches))

    def _first_live_pid(self, *candidates: int | None) -> int | None:
        for pid in candidates:
            if isinstance(pid, int) and self._pid_exists(pid):
                return pid
        return None

    def _discover_running_script_pid(self, script_path: Path, fallback_path: Path | None = None) -> int | None:
        fallback_path = fallback_path or script_path
        lock_pid = self._read_pid_file(fallback_path)
        if isinstance(lock_pid, int) and self._pid_exists(lock_pid):
            return lock_pid
        process_pids = self._iter_python_script_pids(script_path)
        if process_pids:
            return process_pids[0]
        if lock_pid is not None:
            self._clear_pid_file(fallback_path)
        return None

    def _discover_running_script_pids(self, script_path: Path, fallback_path: Path | None = None) -> list[int]:
        fallback_path = fallback_path or script_path
        pids: list[int] = []
        lock_pid = self._read_pid_file(fallback_path)
        if isinstance(lock_pid, int) and self._pid_exists(lock_pid):
            pids.append(lock_pid)
        pids.extend(self._iter_python_script_pids(script_path))
        live = sorted({pid for pid in pids if self._pid_exists(pid)})
        if not live and lock_pid is not None:
            self._clear_pid_file(fallback_path)
        return live

    def _terminate_pid(self, pid: int, *, label: str, timeout: float = 8.0) -> None:
        if not self._pid_exists(pid):
            return
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, **self._windows_subprocess_kwargs())
            output = (self._decode_process_output(result.stdout or b"") + "\n" + self._decode_process_output(result.stderr or b"")).strip()
            time.sleep(0.6)
            low = output.lower()
            if (not self._pid_exists(pid)) or ("не удается найти процесс" in low) or ("не найден" in low) or ("not found" in low):
                return
            raise RuntimeError(f"Не удалось остановить {label} PID {pid}. Детали: {output or 'taskkill failed'}")

        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pid_exists(pid):
                return
            time.sleep(0.2)
        raise RuntimeError(f"Не удалось остановить {label} PID {pid} за {timeout:.1f} сек.")

    def _pid_exists(self, pid: int) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        if psutil is not None:
            try:
                proc = psutil.Process(pid)
                if not proc.is_running():
                    return False
                zombie_status = getattr(psutil, "STATUS_ZOMBIE", None)
                return not (zombie_status is not None and proc.status() == zombie_status)
            except Exception:
                pass
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    check=False,
                    **self._windows_subprocess_kwargs(),
                )
            except Exception:
                return False
            output = self._decode_process_output(result.stdout or b"")
            if "No tasks are running" in output or "Информация:" in output:
                return False
            return f'"{pid}"' in output
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _windows_creation_flags(self, *, hide_window: bool = False) -> int:
        flags = 0
        if os.name != "nt":
            return flags
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if hide_window:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return flags

    def _pid_matches_script(self, pid: int, script_path: Path, *, started_at: float | None = None) -> bool:
        """A saved PID can belong to another process after the worker exits."""
        if not self._pid_exists(pid) or psutil is None:
            return False
        try:
            proc = psutil.Process(pid)
            if started_at is not None and proc.create_time() > float(started_at) + 1:
                return False
            cmdline = proc.cmdline()
            script_resolved = script_path.resolve()
            proc_cwd = Path(proc.cwd()).resolve()
            for index, argument in enumerate(cmdline[1:], start=1):
                if argument == "-m" and index + 1 < len(cmdline):
                    relative = script_resolved.relative_to(self.base_dir.resolve())
                    module_name = ".".join(relative.with_suffix("").parts)
                    return proc_cwd == self.base_dir.resolve() and cmdline[index + 1] == module_name
                candidate = Path(argument)
                if not candidate.is_absolute():
                    candidate = proc_cwd / candidate
                if candidate.resolve() == script_resolved:
                    return True
        except (OSError, ValueError, psutil.Error):
            return False
        return False

    def _get_process_status(self, *, pid_key: str, started_key: str, script_path: Path) -> WorkerStatus:
        state = self.get_control_state()
        pid = state.get(pid_key)
        started_at = state.get(started_key)
        if not isinstance(pid, int) or not self._pid_matches_script(pid, script_path, started_at=started_at):
            return WorkerStatus(running=False, pid=None, started_at=started_at)
        return WorkerStatus(running=True, pid=pid, started_at=started_at, command=[sys.executable, str(script_path)])

    def _launch_python_process(
        self,
        *,
        script_path: Path,
        stdout_path: Path,
        state_pid_key: str,
        state_started_key: str,
        extra_args: list[str] | None = None,
        hide_window: bool = True,
    ) -> WorkerStatus:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._resolve_python_executable(prefer_windowless=hide_window), "-u", str(script_path), *(extra_args or [])]

        env = extend_env_with_site_packages(os.environ.copy())
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env["TG_DISABLE_NOTIFICATIONS"] = "1"
        env.setdefault("WORK_BROWSER_HEADLESS", "1")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.base_dir) if not existing_pythonpath else f"{self.base_dir}{os.pathsep}{existing_pythonpath}"

        with stdout_path.open("a", encoding="utf-8") as stdout_handle:
            kwargs: dict[str, Any] = {
                "cwd": str(self.base_dir),
                "stdout": stdout_handle,
                "stderr": subprocess.STDOUT,
                "env": env,
            }
            if os.name == "nt":
                kwargs.update(self._windows_subprocess_kwargs(hide_window=hide_window))
            else:
                kwargs["start_new_session"] = True

            process = subprocess.Popen(cmd, **kwargs)
        started_at = time.time()
        self.update_control_state({state_pid_key: process.pid, state_started_key: started_at})
        return WorkerStatus(running=True, pid=process.pid, started_at=started_at, command=cmd)

    def _clear_process_state(self, *, pid_key: str, started_key: str, extra_patch: dict[str, Any] | None = None) -> None:
        patch: dict[str, Any] = {pid_key: None, started_key: None}
        if extra_patch:
            patch.update(extra_patch)
        self.update_control_state(patch)

    def _terminate_process(
        self,
        *,
        pid_key: str,
        started_key: str,
        script_path: Path,
        label: str,
        timeout: float = 8.0,
        extra_clear_patch: dict[str, Any] | None = None,
    ) -> WorkerStatus:
        state = self.get_control_state()
        pid = state.get(pid_key)
        if not isinstance(pid, int) or not self._pid_matches_script(pid, script_path, started_at=state.get(started_key)):
            self._clear_process_state(pid_key=pid_key, started_key=started_key, extra_patch=extra_clear_patch)
            return WorkerStatus(running=False, pid=None)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pid_matches_script(pid, script_path, started_at=state.get(started_key)):
                self._clear_process_state(pid_key=pid_key, started_key=started_key, extra_patch=extra_clear_patch)
                return WorkerStatus(running=False, pid=None)
            time.sleep(0.4)

        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, **self._windows_subprocess_kwargs())
            output = (self._decode_process_output(result.stdout or b"") + "\n" + self._decode_process_output(result.stderr or b"")).strip()
            time.sleep(0.6)
            low = output.lower()
            if (not self._pid_exists(pid)) or ("не удается найти процесс" in low) or ("не найден" in low) or ("not found" in low):
                self._clear_process_state(pid_key=pid_key, started_key=started_key, extra_patch=extra_clear_patch)
                return WorkerStatus(running=False, pid=None)
            raise RuntimeError(f"Не удалось остановить {label} PID {pid} за {timeout:.1f} сек. Детали: {output or 'taskkill failed'}")

        os.kill(pid, signal.SIGTERM)
        time.sleep(0.6)
        if self._pid_exists(pid):
            raise RuntimeError(f"Не удалось остановить {label} PID {pid} за {timeout:.1f} сек.")
        self._clear_process_state(pid_key=pid_key, started_key=started_key, extra_patch=extra_clear_patch)
        return WorkerStatus(running=False, pid=None)

    def get_worker_status(self) -> WorkerStatus:
        return self._get_process_status(pid_key="last_run_pid", started_key="last_run_started_at", script_path=self.main_script_path)

    def get_telegram_bot_status(self) -> WorkerStatus:
        state = self.get_control_state()
        tracked_pid = state.get("telegram_bot_pid")
        started_at = state.get("telegram_bot_started_at")
        lock_exists = self.telegram_lock_path.exists()
        if not isinstance(tracked_pid, int) and not lock_exists:
            return WorkerStatus(running=False, pid=None, started_at=started_at)
        live_pid = self._first_live_pid(
            tracked_pid if isinstance(tracked_pid, int) else None,
            self._discover_running_script_pid(self.telegram_script_path, self.telegram_lock_path),
        )
        if live_pid is None:
            if tracked_pid is not None:
                self._clear_process_state(pid_key="telegram_bot_pid", started_key="telegram_bot_started_at")
            return WorkerStatus(running=False, pid=None, started_at=started_at)
        if live_pid != tracked_pid:
            self.update_control_state({"telegram_bot_pid": live_pid, "telegram_bot_started_at": started_at or time.time()})
        self._write_pid_file(self.telegram_lock_path, live_pid)
        return WorkerStatus(running=True, pid=live_pid, started_at=started_at, command=[sys.executable, str(self.telegram_script_path)])

    def _active_profile_name(self) -> str:
        state = self.get_control_state()
        active_name = str(state.get("active_profile") or "").strip()
        profiles = self._read_json(self.profiles_path, {})
        if not active_name and isinstance(profiles, dict) and profiles:
            active_name = next(iter(profiles.keys()))
        return active_name

    def start_worker(self) -> WorkerStatus:
        status = self.get_worker_status()
        if status.running:
            return status
        if not self.main_script_path.exists():
            raise FileNotFoundError(f"Main script not found: {self.main_script_path}")

        active_profile = self._active_profile_name()
        if (self._bot_profile_root(active_profile) / 'browser' / LOGIN_PENDING).exists():
            raise RuntimeError('Завершите ручной вход: закройте отдельный Chrome и нажмите «Проверить вход».')
        self._refresh_runtime_paths(active_profile)
        self.update_control_state({"stop_requested": False})
        started = self._launch_python_process(
            script_path=self.main_script_path,
            stdout_path=self.worker_stdout_path,
            state_pid_key="last_run_pid",
            state_started_key="last_run_started_at",
            extra_args=[active_profile] if active_profile else None,
            hide_window=True,
        )
        return started

    def start_telegram_bot(self) -> WorkerStatus:
        status = self.get_telegram_bot_status()
        if status.running:
            return status
        ready, reason = self.telegram_bot_ready()
        if not ready:
            raise RuntimeError(reason)
        if not self.telegram_script_path.exists():
            raise FileNotFoundError(f"Telegram script not found: {self.telegram_script_path}")

        stray_pid = self._discover_running_script_pid(self.telegram_script_path, self.telegram_lock_path)
        if stray_pid is not None:
            started_at = self.get_control_state().get("telegram_bot_started_at") or time.time()
            self.update_control_state({"telegram_bot_pid": stray_pid, "telegram_bot_started_at": started_at})
            self._write_pid_file(self.telegram_lock_path, stray_pid)
            return WorkerStatus(running=True, pid=stray_pid, started_at=started_at, command=[sys.executable, str(self.telegram_script_path)])

        self._refresh_runtime_paths()
        status = self._launch_python_process(
            script_path=self.telegram_script_path,
            stdout_path=self.telegram_log_path,
            state_pid_key="telegram_bot_pid",
            state_started_key="telegram_bot_started_at",
            hide_window=True,
        )
        if status.pid:
            self._write_pid_file(self.telegram_lock_path, status.pid)
        return status

    def stop_worker(self, timeout: float = 8.0) -> WorkerStatus:
        self.update_control_state({"stop_requested": True})
        return self._terminate_process(
            pid_key="last_run_pid",
            started_key="last_run_started_at",
            script_path=self.main_script_path,
            label="worker",
            timeout=timeout,
            extra_clear_patch={"stop_requested": False},
        )

    def stop_telegram_bot(self, timeout: float = 8.0) -> WorkerStatus:
        state = self.get_control_state()
        tracked_pid = state.get("telegram_bot_pid") if isinstance(state.get("telegram_bot_pid"), int) else None
        pids = self._discover_running_script_pids(self.telegram_script_path, self.telegram_lock_path)
        if tracked_pid and tracked_pid not in pids and self._pid_exists(tracked_pid):
            pids.insert(0, tracked_pid)
        errors: list[str] = []
        for pid in sorted(set(pid for pid in pids if isinstance(pid, int) and pid > 0)):
            try:
                self._terminate_pid(pid, label="Telegram bot", timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        self._clear_pid_file(self.telegram_lock_path)
        self._clear_process_state(pid_key="telegram_bot_pid", started_key="telegram_bot_started_at")
        if errors:
            raise RuntimeError("; ".join(errors))
        return WorkerStatus(running=False, pid=None)

    def restart_worker(self) -> WorkerStatus:
        self.stop_worker()
        time.sleep(0.8)
        return self.start_worker()

    def start_all(self) -> dict[str, WorkerStatus | None]:
        worker = self.start_worker()
        telegram: WorkerStatus | None = None
        ready, _reason = self.telegram_bot_ready()
        if ready:
            telegram = self.start_telegram_bot()
        return {"worker": worker, "telegram": telegram}

    def stop_all(self) -> dict[str, WorkerStatus]:
        telegram = self.stop_telegram_bot()
        worker = self.stop_worker()
        return {"worker": worker, "telegram": telegram}

    def restart_all(self) -> dict[str, WorkerStatus | None]:
        self.stop_all()
        time.sleep(0.8)
        return self.start_all()


    def render_status_text(self) -> str:
        """Return a compact human-readable runtime summary.

        Telegram control bot still calls this legacy formatter directly, so the
        adapter keeps it as a compatibility shim over the richer diagnostics API.
        """
        worker = self.get_worker_status()
        telegram = self.get_telegram_bot_status()
        profiles = self.get_profiles()
        state = self.get_control_state()
        message_stats = self.get_message_pool_stats()
        active_profile = state.get("active_profile") or "—"
        worker_line = "🟢 running" if worker.running else "🔴 stopped"
        telegram_line = "🟢 running" if telegram.running else "🔴 stopped"
        paused_line = "⏸ yes" if state.get("paused") else "▶ no"
        return "\n".join(
            [
                "Состояние проекта",
                f"• Worker: {worker_line}",
                f"• Worker PID: {worker.pid or '—'}",
                f"• Telegram: {telegram_line}",
                f"• Telegram PID: {telegram.pid or '—'}",
                f"• Активный профиль: {active_profile}",
                f"• Пауза: {paused_line}",
                f"• Профилей: {len(profiles)}",
                f"• Сообщений в пуле: {message_stats['count']}",
            ]
        )

    def render_profiles_page(self, page: int = 0, page_size: int = 6) -> tuple[str, list[ProfileEntry], int]:
        """Render a paginated profile list for Telegram control bot menus."""
        profiles = self.get_profiles()
        total_pages = max(1, (len(profiles) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        chunk = profiles[start : start + page_size]
        lines = [f"Профили · страница {page + 1}/{total_pages}"]
        if not chunk:
            lines.append("Профили не найдены.")
        for idx, profile in enumerate(chunk, start=start):
            icon = "🟢" if profile.active else "⚪"
            target_count = int(profile.raw.get("target_count", 0))
            lines.append(f"{idx + 1}. {icon} {profile.label} · targets: {target_count}")
        return "\n".join(lines), chunk, total_pages

    def render_messages_text(self) -> str:
        """Render legacy Telegram summary for the current message pool."""
        stats = self.get_message_pool_stats()
        lines = [
            "Пул сообщений",
            f"• Файл найден: {'да' if stats['exists'] else 'нет'}",
            f"• Сообщений: {stats['count']}",
        ]
        if stats["sample"]:
            lines.append("• Примеры:")
            lines.extend(f"  - {sample[:70]}" for sample in stats["sample"][:3])
        return "\n".join(lines)

    def render_diagnostics_text(self) -> str:
        """Render a short diagnostics summary for Telegram menus."""
        diag = self.diagnostics()
        lines = [
            "Диагностика",
            f"• База проекта: {diag['base_dir']}",
            f"• Worker: {'запущен' if diag['worker']['running'] else 'остановлен'}",
            f"• Telegram: {'запущен' if diag['telegram_bot']['running'] else 'остановлен'}",
            f"• Основной скрипт: {'ok' if diag['files']['main_script'] else 'missing'}",
            f"• Скрипт Telegram: {'ok' if diag['files']['telegram_script'] else 'missing'}",
            f"• control_state.json: {'ok' if diag['files']['control_state'] else 'missing'}",
            f"• profiles.json: {'ok' if diag['files']['profiles'] else 'missing'}",
            f"• Сообщений в пуле: {diag['message_pool']['count']}",
            f"• Ошибок запуска: {len(diag['validation']['critical_errors'])}",
            f"• Предупреждений: {len(diag['validation']['warnings'])}",
            f"• Telegram ready: {'yes' if diag.get('telegram_ready') else 'no'}",
        ]
        return "\n".join(lines)
    def tail_file(self, path: Path, lines: int = 30) -> list[str]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return list(deque((line.rstrip("\n") for line in handle), maxlen=lines))

    def dependency_report(self) -> dict[str, Any]:
        cached_at, cached_report = self._dependency_report_cache
        if cached_report and time.time() - cached_at < 120:
            return dict(cached_report)

        modules = [
            ("playwright", True, "Нужен для браузера TikTok и авторизации."),
            ("psutil", False, "Даёт тихое управление процессами без вспышек cmd."),
            ("requests", False, "Опционально: старый HTTP-клиент, рантайм умеет работать и без него."),
        ]
        items: list[dict[str, Any]] = []
        for module_name, required, hint in modules:
            spec = importlib.util.find_spec(module_name)
            items.append({
                "module": module_name,
                "required": required,
                "installed": spec is not None,
                "hint": hint,
            })
        report = {
            "python": {
                "version": platform.python_version(),
                "executable": sys.executable,
                "platform": platform.platform(),
            },
            "modules": items,
            "commands": {
                "runtime": "pip install playwright && playwright install",
            },
        }
        self._dependency_report_cache = (time.time(), dict(report))
        return report

    def runtime_preflight(self, deps: dict[str, Any] | None = None) -> dict[str, Any]:
        deps = deps or self.dependency_report()
        modules = {item["module"]: item for item in deps["modules"]}
        issues: list[dict[str, Any]] = []

        def add_issue(level: str, title: str, details: str, command: str | None = None) -> None:
            issues.append({"level": level, "title": title, "details": details, "command": command or ""})

        if not self.main_script_path.exists():
            add_issue("critical", "Не найден tiktok_checker.py", f"Основной worker-скрипт отсутствует: {self.main_script_path}")
        if not modules.get("playwright", {}).get("installed"):
            add_issue("critical", "Не установлен playwright", "Worker не сможет открыть TikTok-браузер.", deps["commands"]["runtime"])
        if not self.message_pool_path.exists():
            add_issue("warning", "Нет message_pool.txt", "Будет создан файл по умолчанию, но лучше заполнить его своими сообщениями.")
        if not (self.base_dir / "profiles").exists():
            add_issue("info", "Не найдена папка profiles", "Если профили браузера хранятся отдельно, проверь путь к ним.")
        return {"ok": not any(item["level"] == "critical" for item in issues), "issues": issues}

    def clear_telegram_lock(self) -> None:
        self._clear_pid_file(self.telegram_lock_path)

    def reset_runtime_flags(self) -> dict[str, Any]:
        return self.update_control_state({"stop_requested": False, "paused": False})

    def app_metadata(self) -> dict[str, Any]:
        build_info = self._read_json(self.base_dir / "BUILD_INFO.json", {})
        package_info = self._read_json(self.base_dir / "package.json", {})
        version = "2.2.0"
        if isinstance(package_info, dict):
            version = str(package_info.get("version") or version)
        return {
            "name": "TikTok Heart",
            "version": version,
            "build": build_info.get("build") if isinstance(build_info, dict) else "",
            "notes": build_info.get("notes", []) if isinstance(build_info, dict) else [],
        }

    def live_status(self) -> dict[str, Any]:
        state = self.get_control_state()
        active_profile = str(state.get("active_profile") or "") or None
        worker = self.get_worker_status()
        telegram_status = self.get_telegram_bot_status()
        telegram_ready, telegram_reason = self.telegram_bot_ready()
        profiles = self.get_profiles()
        message_pool = self.get_message_pool_stats()
        run = self.get_current_run_snapshot()
        return {
            "app": self.app_metadata(),
            "mode": "live",
            "updated_at": time.time(),
            "base_dir": str(self.base_dir),
            "telegram_ready": telegram_ready,
            "telegram_ready_reason": telegram_reason,
            "worker": {
                "running": worker.running,
                "pid": worker.pid,
                "started_at": worker.started_at,
                "command": worker.command,
            },
            "telegram_bot": {
                "running": telegram_status.running,
                "pid": telegram_status.pid,
                "started_at": telegram_status.started_at,
                "command": telegram_status.command,
            },
            "state": state,
            "profiles": {
                "total": len(profiles),
                "enabled": sum(1 for profile in profiles if profile.enabled),
                "active": sum(1 for profile in profiles if profile.active),
            },
            "message_pool": message_pool,
            "message_pool_details": message_pool,
            "browser_profile": self.browser_profile_live_status(active_profile),
            "worker_schedule": self.get_worker_schedule_status_cached(max_age_seconds=180.0),
            "run": run,
            "queue": self.queue_snapshot(active_profile),
        }

    def _remove_dir_inside_project(self, path: Path) -> int:
        if not path.exists() or not path.is_dir():
            return 0
        resolved_path = path.resolve()
        resolved_root = self.base_dir.resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"Refusing to remove path outside project: {resolved_path}") from exc
        size = self._dir_size_bytes(path)
        shutil.rmtree(path, ignore_errors=True)
        return size if not path.exists() else 0

    def clean_project_caches(self) -> dict[str, Any]:
        roots = [
            self.base_dir / "yara_app",
            self.base_dir / "tests",
            self.base_dir / "app_shell",
            self.base_dir / "scripts",
        ]
        removed: list[dict[str, Any]] = []
        freed_bytes = 0
        for path in (self.base_dir / ".pytest_cache", self.base_dir / "__pycache__"):
            size = self._remove_dir_inside_project(path)
            if size:
                freed_bytes += size
                removed.append({"path": str(path), "bytes": size})
        for root in roots:
            if not root.exists():
                continue
            candidates = [
                path for path in root.rglob("*")
                if path.is_dir() and path.name in {"__pycache__", ".pytest_cache"}
            ]
            candidates.sort(key=lambda item: len(item.parts), reverse=True)
            for path in candidates:
                size = self._remove_dir_inside_project(path)
                if size:
                    freed_bytes += size
                    removed.append({"path": str(path), "bytes": size})
        return {
            "removed_count": len(removed),
            "removed": removed,
            "freed_bytes": freed_bytes,
        }

    def run_maintenance(self, profile_key: str | None = None) -> dict[str, Any]:
        profile_root = self._bot_profile_root(profile_key)
        actions: list[dict[str, Any]] = []
        freed_bytes = 0

        caches = self.clean_project_caches()
        freed_bytes += int(caches.get("freed_bytes") or 0)
        actions.append({"name": "project_caches", "ok": True, **caches})

        try:
            compact = self.compact_browser_profile(profile_root.name, filter_cookies=True)
            freed_bytes += int(compact.get("freed_bytes") or 0)
            actions.append({"name": "browser_compact", "ok": True, **compact})
        except Exception as exc:  # noqa: BLE001
            actions.append({"name": "browser_compact", "ok": False, "error": str(exc)})

        try:
            backups = self.prune_browser_backups(profile_root.name, keep_latest=1)
            freed_bytes += int(backups.get("freed_bytes") or 0)
            actions.append({"name": "browser_backups", "ok": True, **backups})
        except Exception as exc:  # noqa: BLE001
            actions.append({"name": "browser_backups", "ok": False, "error": str(exc)})

        return {
            "profile_key": profile_root.name,
            "ok": all(bool(item.get("ok")) for item in actions),
            "actions": actions,
            "freed_bytes": freed_bytes,
            "browser_profile": self.browser_profile_summary(profile_root.name),
        }

    def worker_starter_self_test(self, *, no_browser: bool = True) -> dict[str, Any]:
        starter = self.base_dir / "scripts" / "start_worker_once.py"
        if not starter.exists():
            return {"ok": False, "error": f"Starter not found: {starter}", "starter": str(starter)}
        python = self._resolve_python_executable(prefer_windowless=False)
        env = extend_env_with_site_packages(os.environ.copy())
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.base_dir) if not existing_pythonpath else f"{self.base_dir}{os.pathsep}{existing_pythonpath}"
        args = [python, str(starter), "--self-test"]
        if no_browser:
            args.append("--no-browser")
        try:
            result = subprocess.run(
                args,
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
                env=env,
                **self._windows_subprocess_kwargs(hide_window=True),
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "exit_code": None,
                "starter": str(starter),
                "error": f"Self-test timed out after {exc.timeout} seconds",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            }
        try:
            payload = json.loads(result.stdout or "{}")
        except Exception:
            payload = {}
        payload.update({
            "ok": result.returncode == 0 and bool(payload.get("ok", False)),
            "exit_code": result.returncode,
            "starter": str(starter),
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
        return payload

    def _safe_json_check(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"path": str(path), "exists": False, "valid": False, "label": "Файл не найден", "explanation": "Файл отсутствует, приложение не может прочитать настройки из этого JSON."}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            kind = type(payload).__name__
            return {
                "path": str(path),
                "exists": True,
                "valid": True,
                "label": "JSON корректен",
                "kind": kind,
                "explanation": "Это не означает, что значения внутри верные: JSON корректен только синтаксически, а поля всё ещё могут быть пустыми или неверными.",
            }
        except Exception as exc:
            return {
                "path": str(path),
                "exists": True,
                "valid": False,
                "label": "JSON повреждён",
                "error": str(exc),
                "explanation": "Файл существует, но не читается как JSON. Проверь запятые, кавычки и структуру.",
            }


    # ─────────────────────────────────────────────────────────────────────────
    # Health & file-details helpers (used by diagnostics())
    # ─────────────────────────────────────────────────────────────────────────

    def _log_summary(self, path: Path, n: int = 200) -> dict[str, Any]:
        lines = self.tail_file(path, lines=n)
        errors = warnings = successes = 0
        last_error = last_success = None
        latest_result = None
        for line in lines:
            low = line.lower()
            if "error" in low or "[error]" in low:
                errors += 1
                last_error = line[:120]
                latest_result = 'error'
            elif "warning" in low or "[warning]" in low:
                warnings += 1
            positive_count = re.search(r'\bsuccess(?:_count)?\s*[=:]\s*[1-9]\d*\b', low)
            positive_word = re.search(r'\bsuccess(?:ful(?:ly)?)?\b(?!\s*[=:])|\bуспех\b', low)
            if "\u2705" in line or positive_count or positive_word:
                successes += 1
                last_success = line[:120]
                latest_result = 'success'
        return {
            "errors": errors, "warnings": warnings, "successes": successes,
            "last_error": last_error, "last_success": last_success,
            "latest_result": latest_result,
        }

    def _build_health(
        self,
        worker_running: bool,
        telegram_running: bool,
        telegram_ready: bool,
        message_pool: dict[str, Any],
        profiles: list,
        state: dict[str, Any],
        preflight: dict[str, Any],
        browser_profile: dict[str, Any] | None = None,
        worker_schedule: dict[str, Any] | None = None,
        chrome_profiles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        recommendations: list[str] = []
        signals: list[dict[str, Any]] = []

        def add_issue(severity: str, title: str, details: str, recommendation: str | None = None) -> None:
            issues.append({"severity": severity, "title": title, "details": details})
            if recommendation and recommendation not in recommendations:
                recommendations.append(recommendation)

        def add_signal(key: str, label: str, state_label: str, severity: str, details: str) -> None:
            signals.append({
                "key": key,
                "label": label,
                "state": state_label,
                "severity": severity,
                "details": details,
            })

        worker_schedule = worker_schedule or {}
        browser_profile = browser_profile or {}

        # Scores are normalized per category. Optional services should not make
        # the app look broken when the core worker/profile path is healthy.
        runtime_score = 100
        config_score = 100
        content_score = 100
        control_score = 100

        session_invalid = bool(browser_profile.get('session_import_required')) or (browser_profile.get('session_validation') or {}).get('valid') is False
        if session_invalid and not worker_running:
            add_signal("worker", "Worker", "Нужен вход", "warning", "Запуски остановлены до подтверждения новой сессии.")
        elif worker_running:
            add_signal("worker", "Worker", "Запущен", "ok", "Worker сейчас выполняется.")
        elif worker_schedule.get("installed"):
            add_signal("worker", "Worker", "Ожидает", "ok", "Worker остановлен сейчас, но запустится по расписанию.")
        else:
            runtime_score -= 10
            add_signal("worker", "Worker", "Ручной запуск", "info", "Worker не запущен и автозапуск не включён.")

        if state.get("paused"):
            runtime_score -= 25
            add_signal("pause", "Пауза", "Включена", "warning", "Рабочий цикл остановлен флагом paused.")
            add_issue("warning", "Бот поставлен на паузу",
                      "Рабочий цикл остановлен. Нажми «Пауза» ещё раз, чтобы продолжить.",
                      "Сними паузу, если worker должен выполнять задания.")
        elif state.get("dry_run"):
            runtime_score -= 8
            add_signal("mode", "Режим", "DRY RUN", "warning", "Прогон не должен выполнять реальные действия.")
            add_issue("warning", "Включён DRY RUN",
                      "Worker будет работать в тестовом режиме без реальных действий.",
                      "Выключи DRY RUN перед рабочим прогоном.")
        else:
            add_signal("mode", "Режим", "LIVE", "ok", "Реальный режим выполнения включён.")

        critical_preflight = 0
        warning_preflight = 0
        for item in list(preflight.get("issues") or []):
            lvl = item.get("level", "")
            if lvl == "critical":
                critical_preflight += 1
                config_score -= 35
                add_issue("critical", item.get("title", ""), item.get("details", ""),
                          f"{item.get('title')}: {item.get('details')}")
            elif lvl == "warning":
                warning_preflight += 1
                config_score -= 15
                add_issue("warning", item.get("title", ""), item.get("details", ""))
        if critical_preflight:
            add_signal("environment", "Окружение", "Блокер", "critical",
                       f"Критичных проблем preflight: {critical_preflight}.")
        elif warning_preflight:
            add_signal("environment", "Окружение", "Предупреждение", "warning",
                       f"Предупреждений preflight: {warning_preflight}.")
        else:
            add_signal("environment", "Окружение", "Готово", "ok", "Зависимости и основные файлы на месте.")

        msg_count = int(message_pool.get("unique_count") or message_pool.get("count") or 0)
        if msg_count == 0:
            content_score -= 50
            add_signal("messages", "Сообщения", "Пусто", "critical", "В пуле нет сообщений.")
            add_issue("critical", "Пул сообщений пуст",
                      "Добавь хотя бы одно сообщение во вкладке «Сообщения».",
                      "Добавь сообщения в message_pool.txt.")
        elif msg_count < 5:
            content_score -= 15
            add_signal("messages", "Сообщения", "Мало", "warning", f"Найдено {msg_count} сообщений.")
            add_issue("warning", "Мало сообщений в пуле",
                      f"Найдено {msg_count} сообщений. Рекомендуется минимум 5.")
        else:
            add_signal("messages", "Сообщения", "Готово", "ok", f"Уникальных сообщений: {msg_count}.")

        if not profiles:
            content_score -= 30
            add_issue("warning", "Нет профилей",
                      "Создай хотя бы один профиль браузера в папке profiles.",
                      "Создай или импортируй профиль бота.")

        if browser_profile:
            if browser_profile.get('manual_login_pending'):
                content_score -= 60
                add_signal('browser', 'Авторизация', 'Ручной вход', 'critical', 'Worker остановлен до проверки ручного входа.')
                add_issue('critical', 'Ручной вход не завершён',
                          'Закройте отдельное окно Chrome после входа в TikTok.',
                          'В разделе «Браузер» нажмите «Проверить вход».')
            elif session_invalid:
                content_score -= 60
                add_signal("browser", "Авторизация", "Нужен вход", "critical", "Последняя проверка не подтвердила сессию TikTok.")
                add_issue("critical", "Авторизация TikTok не подтверждена",
                          "Файл cookies не доказывает действующий вход. Повторные попытки worker остановлены.",
                          "В разделе «Браузер» откройте отдельный Chrome для входа и выполните проверку.")
            elif browser_profile.get("needs_recovery"):
                content_score -= 60
                add_signal("browser", "Авторизация", "Сломана", "critical",
                           "Старый профиль Chromium не читается после переустановки Windows.")
                add_issue("critical", "Профиль браузера не читается после переустановки Windows",
                          "Windows не может расшифровать старый Chrome/Chromium профиль. Импортируй свежий профиль из Google Chrome.",
                          "Импортируй профиль из Google Chrome во вкладке «Браузер».")
            elif not browser_profile.get("exists") or not browser_profile.get("default_profile_exists"):
                content_score -= 35
                add_signal("browser", "Авторизация", "Нет профиля", "warning",
                           "У worker нет готового browser/user_data.")
                add_issue("warning", "Chromium-профиль бота пуст",
                          "У worker нет готового browser/user_data. Импортируй профиль из Chrome или пройди авторизацию.",
                          "Импортируй TikTok session из Google Chrome или войди в TikTok вручную.")
            elif not browser_profile.get("cookies_exists"):
                content_score -= 20
                add_signal("browser", "Авторизация", "Cookies нет", "warning",
                           "Профиль есть, но cookies не найдены.")
                add_issue("warning", "Cookies не найдены",
                          "В Chromium-профиле нет Cookies DB; авторизация TikTok может отсутствовать.",
                          "Импортируй TikTok session из Google Chrome.")
            if int(browser_profile.get("auth_backoff_left") or 0) > 0:
                content_score -= 25
                add_signal("auth_backoff", "Auth backoff", "Активен", "warning",
                           f"Осталось примерно {int(browser_profile.get('auth_backoff_left') or 0) // 60} мин.")
                add_issue("warning", "TikTok auth временно заблокирован",
                          f"Осталось примерно {int(browser_profile.get('auth_backoff_left') or 0) // 60} мин. Пауза снимается после подтверждения действующей сессии.",
                          "Если в обычном Chrome вход есть, импортируй TikTok session.")
            elif (
                browser_profile.get("exists")
                and browser_profile.get("default_profile_exists")
                and browser_profile.get("cookies_exists")
                and not browser_profile.get("needs_recovery")
                and not session_invalid
                and not browser_profile.get('manual_login_pending')
            ):
                validation = browser_profile.get('session_validation') or {}
                if validation.get('valid') is True:
                    add_signal("browser", "Авторизация", "Проверена ранее", "ok", "Последняя проверка входа успешна. Это не гарантирует, что TikTok не завершил сессию позже.")
                else:
                    add_signal("browser", "Авторизация", "Не проверена", "info", "Cookies сохранены; результат проверки входа ещё не записан. Это не означает потерю авторизации.")
            if browser_profile.get("exists") and (
                not browser_profile.get("local_state_exists", True)
                or not browser_profile.get("preferences_exists")
            ):
                add_signal("profile_metadata", "Файлы профиля", "Не все найдены", "info",
                           "Диагностика не нашла часть служебных файлов. Это не подтверждает отказ входа; работающий профиль переносить заново не нужно.")
        if chrome_profiles is not None and not chrome_profiles:
            add_signal("chrome", "Chrome import", "Недоступен", "info",
                       "Профили Google Chrome не найдены.")

        if not telegram_ready:
            add_signal("telegram", "Telegram", "Не настроен", "info",
                       "Удалённое управление через Telegram недоступно.")
            add_issue("info", "Telegram control не настроен",
                      "Заполни token и chat_id в control/telegram_bot_v2.json.",
                      "Настрой Telegram control, если нужно удалённое управление.")
        elif telegram_running:
            add_signal("telegram", "Telegram", "Запущен", "ok", "Control bot работает.")
        else:
            add_signal("telegram", "Telegram", "Готов", "info",
                       "Настроен, но сейчас не запущен. Это опциональный канал управления и не влияет на готовность worker.")

        if state.get("stop_requested"):
            control_score -= 30
            add_signal("stop_requested", "Stop flag", "Активен", "warning",
                       "Флаг stop_requested остановит рабочий цикл.")
            add_issue("warning", "Установлен stop_requested",
                      "Флаг остановки активен. Сбрось его кнопкой «Сброс флагов».",
                      "Сбрось runtime-флаги.")

        if worker_schedule and worker_schedule.get("available", True) and not worker_schedule.get("installed"):
            control_score -= 15
            add_signal("schedule", "Автозапуск", "Выключен", "info",
                       "Worker не стартует сам через Windows-автозапуск или по расписанию.")
            add_issue("info", "Автозапуск worker выключен",
                      "Worker не стартует сам через Windows-автозапуск или по расписанию.",
                      "Включи автозапуск worker с нужными триггерами.")
        elif worker_schedule and worker_schedule.get("error"):
            control_score -= 15
            add_signal("schedule", "Автозапуск", "Ошибка", "warning",
                       str(worker_schedule.get("error")))
            add_issue("warning", "Не удалось проверить автозапуск worker",
                      str(worker_schedule.get("error")))
        elif worker_schedule and worker_schedule.get("installed") and worker_schedule.get("next_run_stale"):
            control_score -= 20
            next_run = worker_schedule.get("next_run_time") or "unknown"
            add_signal(
                "schedule",
                "Автозапуск",
                "Просрочен",
                "warning",
                f"Windows вернул дату запуска в прошлом: {next_run}.",
            )
            add_issue(
                "warning",
                "Расписание worker просрочено",
                f"Windows Task Scheduler вернул NextRunTime в прошлом: {next_run}. Нужно пересохранить автозапуск.",
                "Открой «Сервисы» и нажми «Сохранить» в блоке автозапуска.",
            )
        elif worker_schedule and worker_schedule.get("installed"):
            next_run = worker_schedule.get("next_run_time") or "по расписанию"
            add_signal("schedule", "Автозапуск", "Включён", "ok",
                       f"Следующий запуск: {next_run}.")

        log_summary_worker   = self._log_summary(self.worker_stdout_path)
        log_summary_telegram = self._log_summary(self.telegram_log_path)
        log_summary_launcher = self._log_summary(self.launcher_log_path)
        recent_success = (log_summary_worker.get("last_success") or
                          log_summary_launcher.get("last_success"))
        recent_error   = (log_summary_worker.get("last_error") or
                          log_summary_launcher.get("last_error"))

        if log_summary_worker.get("errors", 0) > 5:
            runtime_score -= 10
            add_signal("logs", "Логи", "Есть ошибки", "warning",
                       f"{log_summary_worker['errors']} ошибок за последние 200 строк.")
            add_issue("warning", "Много ошибок в worker-логе",
                      f"{log_summary_worker['errors']} ошибок за последние 200 строк.")
        elif recent_error and log_summary_worker.get('latest_result') == 'error':
            add_signal("logs", "Логи", "Последняя запись ошибка", "warning", recent_error[-160:])
        elif recent_success:
            add_signal("logs", "Логи", "Есть успех", "ok", recent_success[-160:])
        elif recent_error:
            add_signal("logs", "Логи", "Есть ошибка", "warning", recent_error[-160:])
        else:
            add_signal("logs", "Логи", "Нет данных", "info", "Свежих успешных событий пока нет.")

        runtime_score = max(0, min(100, runtime_score))
        config_score = max(0, min(100, config_score))
        content_score = max(0, min(100, content_score))
        control_score = max(0, min(100, control_score))
        total = round((runtime_score + config_score + content_score + control_score) / 4)

        has_critical = any(item.get("severity") == "critical" for item in issues)
        has_warning = any(item.get("severity") == "warning" for item in issues)
        if has_critical:
            total = min(total, 64)
        if has_critical or total < 65:
            status = "critical"
            label = "Есть блокер"
            first = next((item for item in issues if item.get("severity") == "critical"), None)
            summary = (first.get("title") if first else "Нужно проверить критичные сигналы.")
        elif has_warning or total < 85:
            status = "warning"
            label = "Нужно внимание"
            first = next((item for item in issues if item.get("severity") == "warning"), None)
            summary = (first.get("title") if first else "Есть предупреждения, но блокеров не найдено.")
        else:
            status = "ok"
            label = "Готово к работе"
            summary = "Локальные проверки пройдены. Статус входа основан на последней проверке, без обращения к TikTok."

        return {
            "score": total,
            "status": status,
            "label": label,
            "summary": summary,
            "issues": issues,
            "recommendations": recommendations,
            "signals": signals,
            "recent_success": recent_success,
            "recent_error": recent_error,
            "breakdown": {
                "runtime": runtime_score,
                "config":  config_score,
                "content": content_score,
                "control": control_score,
            },
            "log_summary": {
                "worker":   log_summary_worker,
                "telegram": log_summary_telegram,
                "launcher": log_summary_launcher,
            },
        }

    def _build_file_details(self) -> list[dict[str, Any]]:
        """Structured list of all project files for the Files tab."""
        run = self.get_current_run_snapshot()
        run_history_path = Path(str(run.get("artifacts_dir"))) / "run_history.jsonl"
        entries: list[tuple[str, Path]] = [
            ("main_script",      self.main_script_path),
            ("telegram_script",  self.telegram_script_path),
            ("message_pool",     self.message_pool_path),
            ("control_state",    self.control_state_path),
            ("profiles",         self.profiles_path),
            ("telegram_config",  self.telegram_config_path),
            ("run_state",        Path(str(run.get("run_state_path")))),
            ("run_summary",      Path(str(run.get("run_summary_path")))),
            ("run_history",      run_history_path),
            ("worker_stdout",    self.worker_stdout_path),
            ("launcher_log",     self.launcher_log_path),
            ("telegram_log",     self.telegram_log_path),
            ("log",              self.log_path),
        ]
        result = []
        json_kinds = {"control_state", "profiles", "telegram_config", "run_state", "run_summary"}
        for kind, file_path in entries:
            exists = file_path.exists()
            stat = file_path.stat() if exists else None
            item = {
                "kind":        kind,
                "path":        str(file_path),
                "exists":      exists,
                "size":        stat.st_size if stat else 0,
                "modified_at": stat.st_mtime if stat else None,
            }
            if kind in json_kinds:
                check = self._safe_json_check(file_path)
                item.update({
                    "kind": "json",
                    "file_kind": kind,
                    "status": check.get("label"),
                    "meaning": check.get("explanation"),
                    "valid": check.get("valid", False),
                    "json_kind": check.get("kind"),
                })
            else:
                item.update({
                    "status": "OK" if exists else "Файл не найден",
                    "meaning": "Файл доступен для чтения." if exists else "Файл отсутствует в проекте.",
                })
            result.append(item)
        return result

    def diagnostics(self) -> dict[str, Any]:
        validation = self.validate_project()
        profiles = self.get_profiles()
        worker = self.get_worker_status()
        telegram_status = self.get_telegram_bot_status()
        state = self.get_control_state()
        message_pool = self.get_message_pool_stats()
        telegram_ready, telegram_reason = self.telegram_bot_ready()
        run = self.get_current_run_snapshot()
        run_history = self.get_run_history(str(state.get("active_profile") or "") or None)
        worker_lifecycle = self.worker_lifecycle_summary(str(state.get("active_profile") or "") or None, limit=12)
        queue = self.queue_snapshot(str(state.get("active_profile") or "") or None)
        dependencies = self.dependency_report()
        preflight = self.runtime_preflight(dependencies)
        browser_profile = self.browser_profile_summary(str(state.get("active_profile") or "") or None)
        try:
            chrome_profiles = self.list_google_chrome_profiles()
        except Exception as exc:  # noqa: BLE001
            chrome_profiles = []
            chrome_profiles_error = str(exc)
        else:
            chrome_profiles_error = ""
        worker_schedule = self.get_worker_schedule_status_cached()
        files = {
            "main_script": self.main_script_path.exists(),
            "telegram_script": self.telegram_script_path.exists(),
            "message_pool": self.message_pool_path.exists(),
            "control_state": self.control_state_path.exists(),
            "profiles": self.profiles_path.exists(),
            "telegram_config": self.telegram_config_path.exists(),
            **{name: path.exists() for name, path in self.log_files().items()},
        }
        health = self._build_health(
            worker_running=worker.running,
            telegram_running=telegram_status.running,
            telegram_ready=telegram_ready,
            message_pool=message_pool,
            profiles=profiles,
            state=state,
            preflight=preflight,
            browser_profile=browser_profile,
            worker_schedule=worker_schedule,
            chrome_profiles=chrome_profiles,
        )
        return {
            "app": self.app_metadata(),
            "base_dir": str(self.base_dir),
            "telegram_ready": telegram_ready,
            "telegram_ready_reason": telegram_reason,
            "validation": validation,
            "worker": {
                "running": worker.running,
                "pid": worker.pid,
                "started_at": worker.started_at,
                "command": worker.command,
            },
            "telegram_bot": {
                "running": telegram_status.running,
                "pid": telegram_status.pid,
                "started_at": telegram_status.started_at,
                "command": telegram_status.command,
            },
            "files": files,
            "locks": {"telegram_lock": self._read_pid_file(self.telegram_lock_path)},
            "json_checks": [
                self._safe_json_check(self.control_state_path),
                self._safe_json_check(self.profiles_path),
                self._safe_json_check(self.telegram_config_path),
            ],
            "dependencies": dependencies,
            "preflight": preflight,
            "browser_profile": browser_profile,
            "chrome_profiles": {
                "total": len(chrome_profiles),
                "items": chrome_profiles,
                "error": chrome_profiles_error,
            },
            "worker_schedule": worker_schedule,
            "profiles": {
                "total": len(profiles),
                "enabled": sum(1 for profile in profiles if profile.enabled),
                "active": sum(1 for profile in profiles if profile.active),
                "items": [
                    {
                        "index": index,
                        "key": profile.key,
                        "label": profile.label,
                        "enabled": profile.enabled,
                        "active": profile.active,
                        "supports_enabled": bool(profile.raw.get("supports_enabled")),
                        "target_count": profile.raw.get("target_count", 0),
                        "targets": [
                            {
                                "name": str(target.get("name") or target.get("url") or ""),
                                "url": str(target.get("url") or target.get("name") or ""),
                                "state": self.get_target_state(
                                    str(target.get("name") or target.get("url") or ""),
                                    profile.key,
                                ),
                            }
                            for target in list(profile.raw.get("targets") or [])
                        ],
                    }
                    for index, profile in enumerate(profiles)
                ],
            },
            "message_pool": message_pool,
            "message_pool_details": message_pool,   # alias used by UI
            "state": state,
            "run": run,
            "run_history": run_history,
            "worker_lifecycle": worker_lifecycle,
            "queue": queue,
            "performance": self.performance_summary(str(state.get("active_profile") or "") or None),
            "file_details": self._build_file_details(),
            "health": health,
            "health_score": health.get("score", 0),
            "issues": list(health.get("issues") or []),
            "recommendations": list(health.get("recommendations") or []),
            "recent_log": self.tail_file(self.log_path),
            "recent_worker_stdout": self.tail_file(self.worker_stdout_path),
            "recent_launcher_log": self.tail_file(self.launcher_log_path),
            "recent_telegram_log": self.tail_file(self.telegram_log_path),
        }

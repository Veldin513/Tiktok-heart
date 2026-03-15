from __future__ import annotations

import importlib.util
import json
import os
import platform
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime_paths import extend_env_with_site_packages
from ttbot.models import legacy_safe_filename, safe_name_key

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
DEFAULT_MAIN_SCRIPT = "tiktok_checker.py"
DEFAULT_TELEGRAM_SCRIPT = "telegram_control_bot.py"
TELEGRAM_CONFIG_NAME = "telegram_bot_v2.json"
TELEGRAM_STATE_NAME = "telegram_bot_v2_state.json"
LAUNCHER_LOG_NAME = "launcher.log"
TELEGRAM_LOG_NAME = "telegram_control_bot_v2.log"
TELEGRAM_LOCK_NAME = "telegram_bot_v2.lock"
COMMON_LOGS_DIR_NAME = "logs"
DESKTOP_STARTUP_LOG_NAME = "desktop_app_startup_error.log"
AUTH_DEBUG_LOG_NAME = "auth_debug.log"


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
    """Project runtime adapter for both the diagnostics UI and the tray shell.

    The adapter intentionally owns all process-management responsibilities so the
    GUI does not need to know how worker/Telegram processes are started,
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
        self.main_script_path = self.base_dir / DEFAULT_MAIN_SCRIPT
        self.telegram_script_path = self.base_dir / DEFAULT_TELEGRAM_SCRIPT
        self.telegram_config_path = self.control_dir / TELEGRAM_CONFIG_NAME
        self.telegram_state_path = self.control_dir / TELEGRAM_STATE_NAME
        self.telegram_lock_path = self.control_dir / TELEGRAM_LOCK_NAME
        self.common_logs_dir = self.base_dir / COMMON_LOGS_DIR_NAME
        self.desktop_startup_error_path = self.common_logs_dir / DESKTOP_STARTUP_LOG_NAME
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.common_logs_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_runtime_paths()

    @staticmethod
    def _discover_project_root(base_dir: Path | str | None) -> Path:
        if base_dir is not None:
            return Path(base_dir).resolve()

        current = Path(__file__).resolve().parent
        candidates = [current, *current.parents]
        markers = (DEFAULT_MAIN_SCRIPT, CONTROL_DIR_NAME, MESSAGE_POOL_NAME)
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

    def _append_file_contents(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        payload = src.read_text(encoding="utf-8", errors="replace")
        if not payload:
            try:
                src.unlink()
            except Exception:
                pass
            return
        prefix = ""
        if dst.exists() and dst.stat().st_size > 0:
            prefix = "\n"
        with dst.open("a", encoding="utf-8") as handle:
            handle.write(prefix + payload)
        try:
            src.unlink()
        except Exception:
            pass

    def _migrate_legacy_log_files(self) -> None:
        mapping = {
            self.base_dir / LOG_NAME: self.log_path,
            self.base_dir / WORKER_STDOUT_NAME: self.worker_stdout_path,
            self.base_dir / LAUNCHER_LOG_NAME: self.launcher_log_path,
            self.base_dir / TELEGRAM_LOG_NAME: self.telegram_log_path,
            self.base_dir / DESKTOP_STARTUP_LOG_NAME: self.desktop_startup_error_path,
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
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_live_mode(self) -> dict[str, Any]:
        """Backward-compatible helper that now only ensures runtime files exist.

        Dry-run mode is controlled explicitly from the desktop UI and should not
        be reset implicitly during startup.
        """
        self.ensure_runtime_files()
        return self.get_control_state()

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
        }

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
        for key, raw in payload.items():
            key_text = str(key)
            targets = self._extract_targets(raw)
            label = key_text
            enabled = key_text == active_name
            if isinstance(raw, dict):
                label = str(raw.get("name") or key_text)
                enabled = bool(raw.get("enabled", enabled))
            items.append(
                ProfileEntry(
                    key=key_text,
                    label=label,
                    enabled=enabled,
                    active=key_text == active_name,
                    raw={"target_count": len(targets), "targets": targets, "source": raw},
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
                    raw={"target_count": len(targets), "targets": targets, "index": index, "source": raw},
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
        profile = profiles[index]
        self.update_control_state({"active_profile": profile.key})
        return self.get_profiles()[index]

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

    def get_target_state(self, target_name: str, profile_key: str | None = None) -> dict[str, Any]:
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

        cooldown_left_h: float = 0.0
        cooldown_left_s: int = 0
        if last_send_at is not None:
            passed = max(0.0, time.time() - last_send_at)
            state = self.get_control_state()
            cooldown_h = float(state.get('cooldown_hours') or 12)
            cooldown_s = cooldown_h * 3600
            left_s = max(0.0, cooldown_s - passed)
            cooldown_left_h = left_s / 3600
            cooldown_left_s = int(left_s)

        existing_last_send = [str(path) for path in paths['last_send'] if path.exists()]
        existing_stats = [str(path) for path in paths['stats'] if path.exists()]

        return {
            'last_send_at': last_send_at,
            'streak_count': streak_count,
            'streak_date': streak_date,
            'cooldown_left_h': round(cooldown_left_h, 2),
            'cooldown_left_s': cooldown_left_s,
            'ready': cooldown_left_s <= 0,
            'state_files': {
                'last_send': existing_last_send,
                'stats': existing_stats,
            },
        }

    def reset_target_cooldown(self, target_name: str, profile_key: str | None = None) -> tuple[int, str]:
        """Delete the active last_send file for a target.

        Finds the first existing last_send file (the one the runtime would read),
        deletes only that file, and returns ``(1, path_str)`` on success or
        ``(0, '')`` if no file was found.
        """
        for path in self._iter_target_state_paths(target_name, profile_key)['last_send']:
            if path.exists():
                try:
                    path.unlink()
                except FileNotFoundError:
                    return 0, ''
                return 1, str(path)
        return 0, ''

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
        return self.set_active_profile(index)

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

    def get_message_pool_stats_for_text(self, text: str) -> dict[str, Any]:
        return self._message_pool_stats_from_text(str(text or ""), exists=True)

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
        backup_dir = self.base_dir / "backups"
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

        return {
            "profile_name": self._active_profile_name(),
            "artifacts_dir": str(artifacts_dir),
            "run_state_path": str(run_state_path),
            "run_summary_path": str(run_summary_path),
            "state_exists": run_state_path.exists(),
            "summary_exists": run_summary_path.exists(),
            "status": str(run_state.get("status") or ("idle" if summary else "unknown")),
            "current_target": run_state.get("current_target"),
            "total_targets": int(run_state.get("total_targets") or summary.get("total_targets") or 0),
            "success_count": int(summary.get("success_count") or 0),
            "skipped_count": int(summary.get("skipped_count") or 0),
            "failed_count": int(summary.get("failed_count") or 0),
            "duration_seconds": float(summary.get("duration_seconds") or 0),
            "results_count": len(results),
            "summary": summary,
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
        script_full = str(script_path).lower()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
            except Exception:
                continue
            lowered = [str(part).lower() for part in cmdline]
            if not lowered:
                continue
            if any(part == script_full or part.endswith(script_name) for part in lowered):
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
                if zombie_status is not None and proc.status() == zombie_status:
                    return False
                return True
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

    def _get_process_status(self, *, pid_key: str, started_key: str, script_path: Path) -> WorkerStatus:
        state = self.get_control_state()
        pid = state.get(pid_key)
        started_at = state.get(started_key)
        if not isinstance(pid, int) or not self._pid_exists(pid):
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
        stdout_handle = open(stdout_path, "a", encoding="utf-8")
        cmd = [self._resolve_python_executable(prefer_windowless=hide_window), "-u", str(script_path), *(extra_args or [])]

        env = extend_env_with_site_packages(os.environ.copy())
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")

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
        label: str,
        timeout: float = 8.0,
        extra_clear_patch: dict[str, Any] | None = None,
    ) -> WorkerStatus:
        state = self.get_control_state()
        pid = state.get(pid_key)
        if not isinstance(pid, int) or not self._pid_exists(pid):
            self._clear_process_state(pid_key=pid_key, started_key=started_key, extra_patch=extra_clear_patch)
            return WorkerStatus(running=False, pid=None)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pid_exists(pid):
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
        self._refresh_runtime_paths(active_profile)
        started = self._launch_python_process(
            script_path=self.main_script_path,
            stdout_path=self.worker_stdout_path,
            state_pid_key="last_run_pid",
            state_started_key="last_run_started_at",
            extra_args=[active_profile] if active_profile else None,
            hide_window=True,
        )
        self.update_control_state({"stop_requested": False})
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

    def restart_telegram_bot(self) -> WorkerStatus:
        self.stop_telegram_bot()
        time.sleep(0.8)
        return self.start_telegram_bot()

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
        modules = [
            ("playwright", True, "Нужен для браузера TikTok и авторизации."),
            ("psutil", False, "Даёт тихое управление процессами без вспышек cmd."),
            ("pystray", False, "Нужен для сворачивания в системный трей."),
            ("PIL", False, "Нужен для иконки трея (пакет Pillow)."),
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
        return {
            "python": {
                "version": platform.python_version(),
                "executable": sys.executable,
                "platform": platform.platform(),
            },
            "modules": items,
            "commands": {
                "desktop": "pip install -r requirements.txt",
                "runtime": "pip install -r requirements.txt && python -m playwright install chromium",
            },
        }

    def runtime_preflight(self) -> dict[str, Any]:
        deps = self.dependency_report()
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
        if not modules.get("pystray", {}).get("installed") or not modules.get("PIL", {}).get("installed"):
            add_issue("info", "Трей недоступен", "Для сворачивания в трей нужны pystray и Pillow.", deps["commands"]["desktop"])
        return {"ok": not any(item["level"] == "critical" for item in issues), "issues": issues}

    def clear_telegram_lock(self) -> None:
        self._clear_pid_file(self.telegram_lock_path)

    def reset_runtime_flags(self) -> dict[str, Any]:
        return self.update_control_state({"stop_requested": False, "paused": False})

    def _safe_json_check(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"path": str(path), "exists": False, "valid": False, "label": "Файл не найден", "explanation": "Файл отсутствует, приложение не может прочитать настройки из этого JSON."}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
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
        for line in lines:
            low = line.lower()
            if "error" in low or "[error]" in low:
                errors += 1
                last_error = line[:120]
            elif "warning" in low or "[warning]" in low:
                warnings += 1
            if "\u2705" in line or "success" in low or "\u0443\u0441\u043f\u0435\u0445" in low:
                successes += 1
                last_success = line[:120]
        return {
            "errors": errors, "warnings": warnings, "successes": successes,
            "last_error": last_error, "last_success": last_success,
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
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        recommendations: list[str] = []

        def add_issue(severity: str, title: str, details: str) -> None:
            issues.append({"severity": severity, "title": title, "details": details})

        # ── runtime (0-30 pts) ────────────────────────────────────────────────
        # Worker запускается по необходимости — не штрафуем за то что он сейчас не активен
        runtime_score = 30
        if not telegram_running:
            runtime_score -= 5
        if state.get("paused"):
            runtime_score -= 5
            add_issue("info", "Бот поставлен на паузу",
                      "Рабочий цикл остановлен. Нажми «⏸ Пауза» ещё раз, чтобы продолжить.")

        # ── config (0-25 pts) ─────────────────────────────────────────────────
        config_score = 25
        for item in list(preflight.get("issues") or []):
            lvl = item.get("level", "")
            if lvl == "critical":
                config_score -= 15
                add_issue("critical", item.get("title", ""), item.get("details", ""))
                recommendations.append(f"{item.get('title')}: {item.get('details')}")
            elif lvl == "warning":
                config_score -= 8
                add_issue("warning", item.get("title", ""), item.get("details", ""))
        config_score = max(0, config_score)

        # ── content (0-25 pts) ────────────────────────────────────────────────
        content_score = 25
        msg_count = int(message_pool.get("unique_count") or message_pool.get("count") or 0)
        if msg_count == 0:
            content_score -= 20
            add_issue("critical", "Пул сообщений пуст",
                      "Добавь хотя бы одно сообщение во вкладке «Сообщения».")
            recommendations.append("Добавь сообщения в message_pool.txt.")
        elif msg_count < 5:
            content_score -= 10
            add_issue("warning", "Мало сообщений в пуле",
                      f"Найдено {msg_count} сообщений. Рекомендуется минимум 5.")
        if not profiles:
            content_score -= 10
            add_issue("warning", "Нет профилей",
                      "Создай хотя бы один профиль браузера в папке profiles.")

        # ── control (0-20 pts) ────────────────────────────────────────────────
        control_score = 20
        if not telegram_ready:
            control_score -= 10
            add_issue("info", "Telegram control не настроен",
                      "Заполни token и chat_id в control/telegram_bot_v2.json.")
            recommendations.append("Настрой Telegram control бота для удалённого управления.")
        if state.get("stop_requested"):
            control_score -= 5
            add_issue("info", "Установлен stop_requested",
                      "Флаг остановки активен. Сбрось его кнопкой «⚑ Сбросить флаги».")
            recommendations.append("Сбрось runtime-флаги (stop_requested = True).")

        total = max(0, min(100,
            runtime_score + config_score + content_score + control_score))

        # ── log summaries ─────────────────────────────────────────────────────
        log_summary_worker   = self._log_summary(self.worker_stdout_path)
        log_summary_telegram = self._log_summary(self.telegram_log_path)
        log_summary_launcher = self._log_summary(self.launcher_log_path)
        recent_success = (log_summary_worker.get("last_success") or
                          log_summary_launcher.get("last_success"))
        recent_error   = (log_summary_worker.get("last_error") or
                          log_summary_launcher.get("last_error"))

        if log_summary_worker.get("errors", 0) > 5:
            add_issue("warning", "Много ошибок в worker-логе",
                      f"{log_summary_worker['errors']} ошибок за последние 200 строк.")

        if total >= 85:
            summary = "Система работает стабильно. Всё в порядке."
        elif total >= 65:
            summary = f"Нужно внимание: {', '.join(r[:60] for r in recommendations[:2]) or 'проверь сигналы ниже'}."
        else:
            summary = f"Обнаружены проблемы: {', '.join(r[:60] for r in recommendations[:2]) or 'проверь сигналы ниже'}."

        return {
            "score": total,
            "summary": summary,
            "issues": issues,
            "recommendations": recommendations,
            "recent_success": recent_success,
            "recent_error": recent_error,
            "breakdown": {
                "runtime": max(0, runtime_score),
                "config":  max(0, config_score),
                "content": max(0, content_score),
                "control": max(0, control_score),
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
        entries: list[tuple[str, Path]] = [
            ("main_script",      self.main_script_path),
            ("telegram_script",  self.telegram_script_path),
            ("message_pool",     self.message_pool_path),
            ("control_state",    self.control_state_path),
            ("profiles",         self.profiles_path),
            ("telegram_config",  self.telegram_config_path),
            ("run_state",        Path(str(run.get("run_state_path")))),
            ("run_summary",      Path(str(run.get("run_summary_path")))),
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
            preflight=self.runtime_preflight(),
        )
        return {
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
            "dependencies": self.dependency_report(),
            "preflight": self.runtime_preflight(),
            "profiles": {
                "total": len(profiles),
                "enabled": sum(1 for profile in profiles if profile.enabled),
                "active": sum(1 for profile in profiles if profile.active),
                "items": [
                    {
                        "key": profile.key,
                        "label": profile.label,
                        "active": profile.active,
                        "target_count": profile.raw.get("target_count", 0),
                    }
                    for profile in profiles
                ],
            },
            "message_pool": message_pool,
            "message_pool_details": message_pool,   # alias used by UI
            "state": state,
            "run": run,
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

from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Target:
    """One recipient in the active profile."""

    name: str
    profile_url: str | None = None


@dataclass(frozen=True)
class CooldownStatus:
    """Cooldown evaluation for a target."""

    allowed: bool
    hours_passed: float | None
    hours_left: float
    seconds_left: int


@dataclass(frozen=True)
class StreakUpdate:
    """Result of updating the streak counter."""

    current_count: int
    is_new_day: bool


class ChatOpenMethod(str, Enum):
    LEGACY_LIST = 'legacy_list'
    STRICT_LIST = 'strict_list'
    PROFILE = 'profile'


@dataclass(frozen=True)
class ChatOpenResult:
    """Result of attempting to open a chat."""

    ok: bool
    method: ChatOpenMethod | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TargetResult:
    """Processing outcome for one target."""

    target: Target
    success: bool
    message: str | None = None
    streak_count: int | None = None
    is_new_day: bool = False
    reason: str | None = None
    chat_method: ChatOpenMethod | None = None
    screenshot_path: Path | None = None


@dataclass
class BrowserRuntimePolicy:
    """Mutable runtime toggles used by Playwright request interception."""

    block_media: bool = True


@dataclass(frozen=True)
class DispatchSettings:
    """Settings that affect one dispatch run."""

    cooldown_hours: int
    retry_attempts: int = 3
    retry_delay_seconds: int = 10
    internet_check_attempts: int = 5
    internet_check_delay_seconds: int = 3
    post_send_delay_seconds: float = 1.5
    final_delay_seconds: int = 2
    dry_run: bool = False
    enable_telegram_notifications: bool = True


@dataclass
class RunSummary:
    """Aggregated result of a full run."""

    profile_name: str
    total_targets: int
    success_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    results: list[TargetResult] = field(default_factory=list)
    duration_seconds: float = 0.0

    def add(self, result: TargetResult) -> None:
        self.results.append(result)
        if result.success:
            self.success_count += 1
        elif result.reason == 'cooldown':
            self.skipped_count += 1
        else:
            self.failed_count += 1

    def add_result(
        self,
        *,
        target: str,
        success: bool,
        message: str | None = None,
        streak_count: int | None = None,
        skipped: bool = False,
        reason: str | None = None,
    ) -> None:
        """Backward-compatible API kept for legacy tiktok_checker.py.

        Before the merge, ``tiktok_checker.py`` used ``models.RunSummary`` from the
        root module, where results were appended via ``add_result(**kwargs)``.
        The merged project now imports ``RunSummary`` from ``ttbot.models``.
        Keeping this wrapper preserves the old call sites without changing the
        dispatch layer that already uses ``add(TargetResult(...))``.
        """
        effective_reason = 'cooldown' if skipped and reason is None else reason
        self.add(
            TargetResult(
                target=Target(name=target),
                success=success,
                message=message,
                streak_count=streak_count,
                reason=effective_reason,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'profile_name': self.profile_name,
            'total_targets': self.total_targets,
            'success_count': self.success_count,
            'skipped_count': self.skipped_count,
            'failed_count': self.failed_count,
            'duration_seconds': round(self.duration_seconds, 2),
            'results': [
                {
                    'target': r.target.name,
                    'profile_url': r.target.profile_url,
                    'success': r.success,
                    'message': r.message,
                    'streak_count': r.streak_count,
                    'is_new_day': r.is_new_day,
                    'reason': r.reason,
                    'skipped': r.reason == 'cooldown',
                    'chat_method': r.chat_method.value if r.chat_method else None,
                    'screenshot_path': str(r.screenshot_path) if r.screenshot_path else None,
                }
                for r in self.results
            ],
        }


"""Unicode-safe helpers for target names.

These helpers keep user-facing names intact while providing a stable internal
representation for comparisons, matching and file-system keys.
"""

_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_NON_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)
_ALLOWED_CONTROL_WHITESPACE = {' ', '\t', '\n', '\r'}


def normalize_display_name(value: str | None) -> str:
    """Return a cleaned display name while preserving its visible meaning.

    - Unicode is normalized with NFKC so compatibility forms become stable.
    - Invisible formatting/control codepoints are removed.
    - Whitespace is collapsed to one space.
    """
    if not value:
        return ''

    normalized = unicodedata.normalize('NFKC', str(value))
    cleaned_chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category in {'Cf', 'Cc', 'Cs'} and char not in _ALLOWED_CONTROL_WHITESPACE:
            continue
        if char == '\xa0':
            char = ' '
        cleaned_chars.append(char)

    collapsed = _WHITESPACE_RE.sub(' ', ''.join(cleaned_chars)).strip()
    return collapsed


def canonical_name(value: str | None) -> str:
    """Return a case-insensitive canonical representation of a name."""
    return normalize_display_name(value).casefold()


def compact_name_token(value: str | None) -> str:
    """Return a comparison token with punctuation and spaces removed."""
    canonical = canonical_name(value)
    return _NON_ALNUM_RE.sub('', canonical)


def build_name_variants(value: str | None) -> list[str]:
    """Return deduplicated variants useful for UI text matching."""
    raw = '' if value is None else str(value)
    variants: list[str] = []
    for candidate in (
        raw,
        raw.strip(),
        normalize_display_name(raw),
        canonical_name(raw),
        compact_name_token(raw),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def names_match(candidate_text: str | None, target_name: str | None) -> bool:
    """Compare two names with Unicode normalization and loose punctuation rules."""
    candidate_canonical = canonical_name(candidate_text)
    target_canonical = canonical_name(target_name)
    if not candidate_canonical or not target_canonical:
        return False

    if candidate_canonical == target_canonical:
        return True
    if target_canonical in candidate_canonical:
        return True

    candidate_compact = compact_name_token(candidate_text)
    target_compact = compact_name_token(target_name)
    if not candidate_compact or not target_compact:
        return False
    return candidate_compact == target_compact or target_compact in candidate_compact


def legacy_safe_filename(value: str | None) -> str:
    """Historical file-name strategy kept for backwards compatibility."""
    raw = '' if value is None else str(value)
    safe_name = ''.join(c for c in raw if c.isalnum() or c in ('_', '-')).rstrip()
    return safe_name or 'target'


def safe_name_key(value: str | None) -> str:
    """Build a cross-platform stable file key for a target name.

    The visible part stays human-readable when possible, while a short hash keeps
    names unique even after normalization.
    """
    normalized = normalize_display_name(value)
    ascii_slug_source = unicodedata.normalize('NFKD', normalized)
    ascii_slug = ascii_slug_source.encode('ascii', 'ignore').decode('ascii')
    ascii_slug = _NON_ALNUM_RE.sub('_', ascii_slug).strip('_').lower()
    digest_source = normalized or 'target'
    digest = hashlib.sha1(digest_source.encode('utf-8')).hexdigest()[:10]
    prefix = ascii_slug[:40] if ascii_slug else 'target'
    return f'{prefix}_{digest}'


@dataclass(frozen=True)
class NotificationSettings:
    token: str
    chat_ids: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class AppSettings:
    dispatch: DispatchSettings
    messages: tuple[str, ...]
    notifications: NotificationSettings


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    targets: tuple[Target, ...]


def build_settings(*, dry_run: bool = False, telegram_enabled: bool = True) -> AppSettings:
    """Build application settings from config constants and the live message pool.

    Messages are loaded from the file-based pool via ControlStore so there is a
    single loading path shared with tiktok_checker. config constants remain the
    source of truth for everything else.
    """
    messages = tuple(ControlStore().load_messages())

    return AppSettings(
        dispatch=DispatchSettings(
            cooldown_hours=DEFAULT_COOLDOWN_HOURS,
            dry_run=dry_run,
            enable_telegram_notifications=telegram_enabled,
        ),
        messages=messages,
        notifications=NotificationSettings(
            token=TG_TOKEN,
            chat_ids=tuple(TG_CHAT_IDS),
            enabled=telegram_enabled,
        ),
    )


def build_profile_config(profile_name: str, raw_targets: Iterable[dict]) -> ProfileConfig:
    targets = tuple(
        Target(name=item['name'], profile_url=item.get('url'))
        for item in raw_targets
    )
    return ProfileConfig(name=profile_name, targets=targets)


class StateStore:
    def __init__(self, state_dir: str | Path, cooldown_hours: int):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown_hours = cooldown_hours

    @staticmethod
    def _legacy_safe_name(target_name: str) -> str:
        return legacy_safe_filename(target_name)

    @staticmethod
    def _stable_key(target_name: str) -> str:
        return safe_name_key(target_name)

    def get_target_files(self, target_name: str) -> tuple[Path, Path, Path, Path]:
        stable = self._stable_key(target_name)
        legacy = self._legacy_safe_name(target_name)
        return (
            self.state_dir / f'last_send_{stable}.txt',
            self.state_dir / f'stats_{stable}.txt',
            self.state_dir / f'last_send_{legacy}.txt',
            self.state_dir / f'stats_{legacy}.txt',
        )

    def _read_timestamp(self, path_a: Path, path_b: Path) -> float | None:
        for path in (path_a, path_b):
            if path.exists():
                try:
                    return float(path.read_text(encoding='utf-8').strip())
                except Exception:
                    continue
        return None

    def get_last_send_at(self, target_name: str) -> float | None:
        log_file, _, legacy_log, _ = self.get_target_files(target_name)
        return self._read_timestamp(log_file, legacy_log)

    def get_cooldown_status(self, target_name: str) -> CooldownStatus:
        sent_at = self.get_last_send_at(target_name)
        if sent_at is None:
            return CooldownStatus(True, None, 0, 0)
        seconds_passed = max(0, time.time() - sent_at)
        cooldown_seconds = self.cooldown_hours * 3600
        seconds_left = max(0, cooldown_seconds - seconds_passed)
        return CooldownStatus(seconds_left <= 0, seconds_passed / 3600, seconds_left / 3600, int(seconds_left))

    def mark_sent_now(self, target_name: str) -> None:
        log_file, _, _, _ = self.get_target_files(target_name)
        log_file.write_text(str(time.time()), encoding='utf-8')

    def get_streak_count(self, target_name: str) -> int:
        _, stats_file, _, legacy_stats = self.get_target_files(target_name)
        for path in (stats_file, legacy_stats):
            if path.exists():
                try:
                    parts = path.read_text(encoding='utf-8').strip().split('|')
                    return int(parts[0]) if parts and parts[0] else 0
                except Exception:
                    continue
        return 0

    def update_streak_stats(self, target_name: str) -> StreakUpdate:
        _, stats_file, _, legacy_stats = self.get_target_files(target_name)
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        current_count = 0
        last_send_date = ''
        for path in (stats_file, legacy_stats):
            if path.exists():
                try:
                    parts = path.read_text(encoding='utf-8').strip().split('|')
                    current_count = int(parts[0]) if parts and parts[0] else 0
                    last_send_date = parts[1] if len(parts) > 1 else ''
                    break
                except Exception:
                    current_count = 0
                    last_send_date = ''
        is_new_day = last_send_date != today
        if is_new_day:
            current_count += 1
        stats_file.write_text(f'{current_count}|{today}', encoding='utf-8')
        return StreakUpdate(current_count=current_count, is_new_day=is_new_day)


from config import (
    CONTROL_DIR,
    CONTROL_STATE_FILE,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_MESSAGES,
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILES,
    MESSAGE_POOL_FILE,
    PROFILES_FILE,
)


@dataclass
class ControlState:
    active_profile: str
    cooldown_hours: int
    dry_run: bool
    paused: bool
    stop_requested: bool
    last_run_pid: int | None = None
    last_run_started_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'active_profile': self.active_profile,
            'cooldown_hours': self.cooldown_hours,
            'dry_run': self.dry_run,
            'paused': self.paused,
            'stop_requested': self.stop_requested,
            'last_run_pid': self.last_run_pid,
            'last_run_started_at': self.last_run_started_at,
        }


class ControlStore:
    def __init__(self) -> None:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        self._seed_files()

    def _seed_files(self) -> None:
        if not PROFILES_FILE.exists():
            PROFILES_FILE.write_text(json.dumps(DEFAULT_PROFILES, ensure_ascii=False, indent=2), encoding='utf-8')
        if not CONTROL_STATE_FILE.exists():
            state = ControlState(
                active_profile=DEFAULT_PROFILE_NAME,
                cooldown_hours=DEFAULT_COOLDOWN_HOURS,
                dry_run=False,
                paused=False,
                stop_requested=False,
            )
            CONTROL_STATE_FILE.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
        if not MESSAGE_POOL_FILE.exists():
            MESSAGE_POOL_FILE.write_text("\n".join(DEFAULT_MESSAGES) + "\n", encoding='utf-8')

    def load_profiles(self) -> dict[str, list[dict[str, str]]]:
        return json.loads(PROFILES_FILE.read_text(encoding='utf-8'))

    def save_profiles(self, profiles: dict[str, list[dict[str, str]]]) -> None:
        PROFILES_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding='utf-8')

    def load_state(self) -> ControlState:
        raw = json.loads(CONTROL_STATE_FILE.read_text(encoding='utf-8'))
        return ControlState(
            active_profile=raw.get('active_profile', DEFAULT_PROFILE_NAME),
            cooldown_hours=int(raw.get('cooldown_hours', DEFAULT_COOLDOWN_HOURS)),
            dry_run=bool(raw.get('dry_run', False)),
            paused=bool(raw.get('paused', False)),
            stop_requested=bool(raw.get('stop_requested', False)),
            last_run_pid=raw.get('last_run_pid'),
            last_run_started_at=raw.get('last_run_started_at'),
        )

    def save_state(self, state: ControlState) -> None:
        CONTROL_STATE_FILE.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')

    def update_state(self, **changes: Any) -> ControlState:
        state = self.load_state()
        data = state.to_dict()
        data.update(changes)
        new_state = ControlState(
            active_profile=data['active_profile'],
            cooldown_hours=int(data['cooldown_hours']),
            dry_run=bool(data['dry_run']),
            paused=bool(data['paused']),
            stop_requested=bool(data['stop_requested']),
            last_run_pid=data.get('last_run_pid'),
            last_run_started_at=data.get('last_run_started_at'),
        )
        self.save_state(new_state)
        return new_state

    def ensure_profile(self, profile_name: str) -> None:
        profiles = self.load_profiles()
        if profile_name not in profiles:
            raise ValueError(f'Профиль {profile_name!r} не найден')

    def set_active_profile(self, profile_name: str) -> ControlState:
        self.ensure_profile(profile_name)
        return self.update_state(active_profile=profile_name)

    def list_targets(self, profile_name: str | None = None) -> list[dict[str, str]]:
        profiles = self.load_profiles()
        profile = profile_name or self.load_state().active_profile
        return list(profiles.get(profile, []))

    def add_profile(self, profile_name: str) -> None:
        profiles = self.load_profiles()
        profiles.setdefault(profile_name, [])
        self.save_profiles(profiles)

    def remove_profile(self, profile_name: str) -> None:
        profiles = self.load_profiles()
        if profile_name not in profiles:
            raise ValueError('Профиль не найден')
        if profile_name == self.load_state().active_profile:
            raise ValueError('Нельзя удалить активный профиль')
        del profiles[profile_name]
        self.save_profiles(profiles)

    def add_target(self, profile_name: str, name: str, url: str | None) -> None:
        profiles = self.load_profiles()
        profiles.setdefault(profile_name, [])
        exists = any(item.get('name') == name for item in profiles[profile_name])
        if exists:
            raise ValueError('Адресат уже существует')
        entry = {'name': name}
        if url:
            entry['url'] = url
        profiles[profile_name].append(entry)
        self.save_profiles(profiles)

    def remove_target(self, profile_name: str, name: str) -> None:
        profiles = self.load_profiles()
        items = profiles.get(profile_name, [])
        new_items = [item for item in items if item.get('name') != name]
        if len(new_items) == len(items):
            raise ValueError('Адресат не найден')
        profiles[profile_name] = new_items
        self.save_profiles(profiles)

    def load_messages(self) -> list[str]:
        lines = [line.strip() for line in MESSAGE_POOL_FILE.read_text(encoding='utf-8').splitlines()]
        lines = [line for line in lines if line and not line.startswith('#')]
        out: list[str] = []
        seen = set()
        for item in lines:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def save_messages(self, messages: list[str]) -> None:
        cleaned: list[str] = []
        seen = set()
        for item in messages:
            text = item.strip()
            if text and text not in seen:
                seen.add(text)
                cleaned.append(text)
        MESSAGE_POOL_FILE.write_text("\n".join(cleaned) + "\n", encoding='utf-8')

    def add_message(self, text: str) -> None:
        messages = self.load_messages()
        if text in messages:
            raise ValueError('Сообщение уже существует')
        messages.append(text)
        self.save_messages(messages)

    def remove_message(self, text: str) -> None:
        messages = self.load_messages()
        new_messages = [item for item in messages if item != text]
        if len(new_messages) == len(messages):
            raise ValueError('Сообщение не найдено')
        self.save_messages(new_messages)

    def request_stop(self) -> ControlState:
        return self.update_state(stop_requested=True)

    def clear_stop(self) -> ControlState:
        return self.update_state(stop_requested=False)

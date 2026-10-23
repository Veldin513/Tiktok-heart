
from __future__ import annotations

import atexit
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from yara_app.auth_flow import (
    auth_backoff_seconds_left,
    clear_auth_backoff,
    init_work_browser,
    is_logged_in,
    record_session_validation,
    safe_close_context,
    wait_for_authenticated_page,
)
from yara_app.config import BOT_NAME, TG_CHAT_IDS, TG_DISABLE_NOTIFICATIONS, TG_TOKEN, MessageSelector, get_cli_profile
from yara_app.project_adapter import ProjectAdapter
from yara_app.runtime_paths import (
    RunLock,
    browser_profile_needs_recovery,
    build_profile_paths,
    configure_logging,
    format_duration,
)
from yara_app.tiktok_messenger import open_chat_by_list, open_chat_by_profile, open_messages, send_message
from yara_app.ttbot.models import ControlStore, RunSummary, StateStore, Target, safe_name_key

try:
    import requests  # type: ignore
except Exception:  # noqa: BLE001
    requests = None

logger = logging.getLogger(__name__)


@dataclass
class TelegramClient:
    token: str
    chat_ids: list[str]
    enabled: bool = True

    def __post_init__(self) -> None:
        self.base_url = f'https://api.telegram.org/bot{self.token}' if self.token else ''
        self.session = requests.Session() if requests is not None else None
        if requests is None and self.enabled and self.token:
            logger.info('requests не установлен; TelegramClient работает через urllib fallback.')

    def _post(self, method: str, *, data: dict[str, Any] | None = None, json_payload: dict[str, Any] | None = None, files: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any] | None:
        if not self.enabled or not self.token:
            return None
        if self.session is not None:
            try:
                response = self.session.post(f'{self.base_url}/{method}', data=data, json=json_payload, files=files, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                logger.warning('Telegram API %s failed: %s', method, exc)
                return None
        if files:
            logger.warning('Telegram API %s skipped: multipart upload requires requests, которого нет в окружении.', method)
            return None
        encoded: bytes | None = None
        headers = {}
        if json_payload is not None:
            encoded = json.dumps(json_payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif data is not None:
            encoded = urllib.parse.urlencode({k: str(v) for k, v in data.items()}).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(f'{self.base_url}/{method}', data=encoded, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as exc:
            logger.warning('Telegram API %s failed: %s', method, exc)
            return None

    def _get(self, method: str, *, params: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any] | None:
        if not self.enabled or not self.token:
            return None
        if self.session is not None:
            try:
                response = self.session.get(f'{self.base_url}/{method}', params=params, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                logger.warning('Telegram API %s failed: %s', method, exc)
                return None
        query = urllib.parse.urlencode({k: str(v) for k, v in (params or {}).items()})
        request = urllib.request.Request(f'{self.base_url}/{method}' + (f'?{query}' if query else ''), method='GET')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as exc:
            logger.warning('Telegram API %s failed: %s', method, exc)
            return None

    def send_text(self, text: str, *, chat_ids: list[str] | None = None, reply_markup: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        targets = chat_ids or self.chat_ids
        for chat_id in targets:
            payload: dict[str, Any] = {'chat_id': chat_id, 'text': text}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            self._post('sendMessage', json_payload=payload)

    def send_photo(self, photo_path: str | Path, caption: str | None = None) -> None:
        if not self.enabled:
            return
        photo_path = Path(photo_path)
        if not photo_path.exists():
            return
        for chat_id in self.chat_ids:
            with photo_path.open('rb') as photo_file:
                data = {'chat_id': chat_id}
                if caption:
                    data['caption'] = caption
                self._post('sendPhoto', data=data, files={'photo': photo_file}, timeout=30)

    def send_photo_with_keyboard(self, photo_path: str | Path, caption: str, keyboard: dict[str, Any]) -> list[tuple[str, int]]:
        sent_messages: list[tuple[str, int]] = []
        if not self.enabled:
            return sent_messages
        photo_path = Path(photo_path)
        if not photo_path.exists():
            return sent_messages
        for chat_id in self.chat_ids:
            with photo_path.open('rb') as photo_file:
                response = self._post('sendPhoto', data={'chat_id': chat_id, 'caption': caption, 'reply_markup': json.dumps(keyboard, ensure_ascii=False)}, files={'photo': photo_file}, timeout=30)
            if response and response.get('ok') and response.get('result'):
                sent_messages.append((chat_id, response['result']['message_id']))
        return sent_messages

    def poll_updates(self, offset: int | None = None, timeout: int = 10) -> list[dict[str, Any]]:
        if not self.enabled or not self.token:
            return []
        params: dict[str, Any] = {'timeout': timeout}
        if offset is not None:
            params['offset'] = offset
        payload = self._get('getUpdates', params=params, timeout=timeout + 5)
        if payload and payload.get('ok'):
            return payload.get('result', [])
        return []

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload = {'callback_query_id': callback_query_id}
        if text:
            payload['text'] = text
        self._post('answerCallbackQuery', json_payload=payload)

    def clear_markup(self, chat_id: str, message_id: int) -> None:
        self._post('editMessageReplyMarkup', json_payload={'chat_id': chat_id, 'message_id': message_id, 'reply_markup': {'inline_keyboard': []}})

    def send_run_started(self, *, bot_name: str, profile_name: str, target_count: int, dry_run: bool = False) -> None:
        tag = '🧪 DRY RUN\n' if dry_run else ''
        self.send_text(f'{tag}🤖 {bot_name} ({profile_name}) запущен.\n🎯 Адресатов: {target_count}')

    def send_run_finished(self, *, profile_name: str, success_count: int, skipped_count: int, failed_count: int, duration_text: str) -> None:
        self.send_text(f'🏁 Завершено: {profile_name}\n✅ Успешно: {success_count}\n⏭ Пропущено: {skipped_count}\n❌ Ошибок: {failed_count}\n⏱ Длительность: {duration_text}')

    def send_target_success(self, *, target_name: str, message: str, streak_count: int, is_new_day: bool) -> None:
        status_text = 'Огонек вырос! 🔥' if is_new_day else 'Активность продлена.'
        self.send_text(f'✅ Сообщение для {target_name} доставлено!\n💬 Текст: {message}\n{status_text}\n🔥 Всего дней: {streak_count}')

control_store = ControlStore()
state = control_store.load_state()
ACTIVE_PROFILE = get_cli_profile() or state.active_profile
control_store.ensure_profile(ACTIVE_PROFILE)
PATHS = build_profile_paths(ACTIVE_PROFILE)
logger = configure_logging(PATHS)

tg = TelegramClient(TG_TOKEN, TG_CHAT_IDS, enabled=not TG_DISABLE_NOTIFICATIONS)
store = StateStore(PATHS.state_dir, state.cooldown_hours)
run_lock = RunLock(PATHS.run_lock_file)
message_selector = MessageSelector(control_store.load_messages())
auth_coord_adapter = ProjectAdapter()
RUN_STATE_FILE = PATHS.artifacts_dir / 'run_state.json'
RUN_HISTORY_FILE = PATHS.artifacts_dir / 'run_history.jsonl'
RUN_HISTORY_MAX_BYTES = 1_500_000
RUN_HISTORY_KEEP_BYTES = 500_000
AUTH_BACKOFF_FILE = PATHS.state_dir / 'auth_backoff.json'
TARGET_GUARD_DIR = PATHS.state_dir / 'target_send_guards'
TARGET_SEND_GUARD_SECONDS = max(30, int(os.getenv('TIKTOK_TARGET_GUARD_SECONDS', '300')))
CAPTCHA_SELECTORS = (
    '#captcha-verify-image',
    '.captcha-disable-scroll',
    'div[id^="secsdk-captcha"]',
    '#arkose-iframe',
    'iframe[src*="captcha"]',
)


def _suspend_control_bot_for_auth() -> bool:
    try:
        status = auth_coord_adapter.get_telegram_bot_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning('Не удалось проверить Telegram control bot перед авторизацией: %s', exc)
        return False
    if not status.running:
        return False
    logger.warning('Временная остановка Telegram control bot на время авторизации, чтобы избежать Telegram 409 Conflict.')
    try:
        auth_coord_adapter.stop_telegram_bot(timeout=8.0)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error('Не удалось временно остановить Telegram control bot перед авторизацией: %s', exc)
        return False


def _resume_control_bot_after_auth(was_suspended: bool) -> None:
    if not was_suspended:
        return
    try:
        ready, reason = auth_coord_adapter.telegram_bot_ready()
        if not ready:
            logger.warning('Telegram control bot не был перезапущен после авторизации: %s', reason)
            return
        auth_coord_adapter.start_telegram_bot()
        logger.info('Telegram control bot перезапущен после авторизации.')
    except Exception as exc:  # noqa: BLE001
        logger.error('Не удалось перезапустить Telegram control bot после авторизации: %s', exc)


def _write_run_state(**data) -> None:
    payload = {
        'profile_name': ACTIVE_PROFILE,
        'pid': os.getpid(),
        'timestamp': time.time(),
        **data,
    }
    RUN_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def trim_run_history(path: Path, *, max_bytes: int = RUN_HISTORY_MAX_BYTES, keep_bytes: int = RUN_HISTORY_KEEP_BYTES) -> None:
    """Keep history bounded without splitting a JSONL record in half."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        with path.open('rb') as handle:
            handle.seek(-min(keep_bytes, path.stat().st_size), os.SEEK_END)
            payload = handle.read()
        separator = payload.find(b'\n')
        if separator < 0:
            return
        payload = payload[separator + 1:]
        temporary = path.with_suffix(f'{path.suffix}.tmp')
        temporary.write_bytes(payload)
        temporary.replace(path)
    except OSError as exc:
        logger.warning('Не удалось сократить run history: %s', exc)


def _append_run_history(event: str, **data: Any) -> None:
    payload = {
        'timestamp': time.time(),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'profile_name': ACTIVE_PROFILE,
        'event': event,
        **data,
    }
    try:
        RUN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RUN_HISTORY_FILE.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
        trim_run_history(RUN_HISTORY_FILE)
    except Exception as exc:
        logger.warning('Не удалось записать run history: %s', exc)

def _compact_payload(data: dict[str, Any]) -> str:
    parts = []
    for key, value in data.items():
        if value is None:
            continue
        text = str(value)
        if len(text) > 120:
            text = text[:117] + '...'
        parts.append(f'{key}={text}')
    return ' '.join(parts)


def log_lifecycle(stage: str, **data: Any) -> None:
    logger.info('[lifecycle] %s %s', stage, _compact_payload(data))
    _append_run_history('lifecycle', stage=stage, **data)


@contextmanager
def timed_phase(phase: str, **data: Any):
    started = time.perf_counter()
    logger.info('[phase:%s] start %s', phase, _compact_payload(data))
    ok = False
    error = ''
    try:
        yield
        ok = True
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        payload = {**data, 'elapsed_ms': elapsed_ms, 'ok': ok}
        if error:
            payload['error'] = error[:300]
        logger.info('[phase:%s] finish %sms ok=%s', phase, elapsed_ms, ok)
        _append_run_history('phase_timing', phase=phase, **payload)


def detect_captcha(page, *, timeout_ms: int = 700) -> dict[str, Any]:
    for selector in CAPTCHA_SELECTORS:
        try:
            if page.locator(selector).first.is_visible(timeout=timeout_ms):
                return {'found': True, 'selector': selector}
        except Exception:
            continue
    return {'found': False, 'selector': ''}


def wait_for_internet() -> bool:
    for _ in range(5):
        try:
            urllib.request.urlopen('https://www.google.com', timeout=5).close()
            return True
        except Exception:
            time.sleep(3)
    logger.error('Нет интернета. Завершение.')
    return False


def save_run_summary(summary: RunSummary) -> None:
    (PATHS.artifacts_dir / 'run_summary.json').write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')


def send_start_message(targets: list[Target], *, dry_run: bool) -> None:
    tg.send_run_started(bot_name=BOT_NAME, profile_name=ACTIVE_PROFILE, target_count=len(targets), dry_run=dry_run)


def force_send_on_boot() -> bool:
    return str(os.getenv('WORKER_FORCE_SEND_ON_BOOT') or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def ready_targets_for_run(targets: list[Target], summary: RunSummary) -> list[Target]:
    ready: list[Target] = []
    boot_force = force_send_on_boot()
    for target in targets:
        cooldown = store.get_cooldown_status(target.name)
        if cooldown.allowed:
            ready.append(target)
            continue
        if boot_force:
            logger.info('[%s] Cooldown preflight bypassed by PC startup exception. Left %s.', target.name, format_duration(cooldown.seconds_left))
            _append_run_history(
                'target_cooldown_bypass',
                target=target.name,
                reason='pc_startup',
                seconds_left=cooldown.seconds_left,
                preflight=True,
            )
            ready.append(target)
            continue
        passed_text = 'unknown' if cooldown.hours_passed is None else format_duration(int(cooldown.hours_passed * 3600))
        left_text = format_duration(cooldown.seconds_left)
        logger.info('[%s] Cooldown preflight: passed %s, left %s. Browser start skipped.', target.name, passed_text, left_text)
        summary.add_result(target=target.name, success=False, skipped=True, reason='cooldown')
        _append_run_history(
            'target_result',
            target=target.name,
            success=False,
            skipped=True,
            reason='cooldown',
            seconds_left=cooldown.seconds_left,
            preflight=True,
        )
    return ready


def target_guard_path(target_name: str) -> Path:
    return TARGET_GUARD_DIR / f'{safe_name_key(target_name)}.json'


def acquire_target_send_guard(target_name: str) -> tuple[bool, dict[str, Any]]:
    TARGET_GUARD_DIR.mkdir(parents=True, exist_ok=True)
    path = target_guard_path(target_name)
    now = time.time()
    payload = {
        'target': target_name,
        'profile_name': ACTIVE_PROFILE,
        'pid': os.getpid(),
        'started_at': now,
        'ttl_seconds': TARGET_SEND_GUARD_SECONDS,
    }
    for attempt in range(2):
        try:
            with path.open('x', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            return True, {'path': str(path), **payload}
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding='utf-8-sig'))
            except Exception:
                existing = {}
            started_at = float(existing.get('started_at') or 0)
            age = now - started_at if started_at else TARGET_SEND_GUARD_SECONDS + 1
            if age >= TARGET_SEND_GUARD_SECONDS:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    if attempt == 1:
                        return False, {'path': str(path), 'reason': 'stale_guard_unlink_failed', 'age_seconds': round(age, 1)}
                continue
            return False, {
                'path': str(path),
                'reason': 'recent_guard',
                'age_seconds': round(age, 1),
                'left_seconds': max(0, int(TARGET_SEND_GUARD_SECONDS - age)),
                'pid': existing.get('pid'),
            }
    return False, {'path': str(path), 'reason': 'guard_create_failed'}


def mark_target_guard_sent(target_name: str) -> None:
    path = target_guard_path(target_name)
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    except Exception:
        payload = {}
    payload.update({'sent_at': time.time(), 'status': 'sent'})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        logger.warning('Failed to mark target send guard for %s: %s', target_name, exc)


def stop_requested() -> bool:
    return control_store.load_state().stop_requested


def stop_or_pause_reason() -> str | None:
    state = control_store.load_state()
    if state.stop_requested:
        return 'stop_requested'
    if state.paused:
        return 'paused'
    return None


def process_target(page, target: Target, summary: RunSummary) -> None:
    _write_run_state(status='running', current_target=target.name)
    logger.info('Начало обработки адресата: %s | profile=%s', target.name, target.profile_url)
    with timed_phase('target_cooldown_check', target=target.name):
        cooldown = store.get_cooldown_status(target.name)
    if not cooldown.allowed:
        if force_send_on_boot():
            logger.info('[%s] Cooldown bypassed by PC startup exception. Left %s.', target.name, format_duration(cooldown.seconds_left))
            _append_run_history('target_cooldown_bypass', target=target.name, reason='pc_startup', seconds_left=cooldown.seconds_left)
        else:
            passed_text = 'неизвестно' if cooldown.hours_passed is None else format_duration(int(cooldown.hours_passed * 3600))
            left_text = format_duration(cooldown.seconds_left)
            logger.info('[%s] Рано. Прошло %s. Осталось %s.', target.name, passed_text, left_text)
            summary.add_result(target=target.name, success=False, skipped=True, reason='cooldown')
            _append_run_history('target_result', target=target.name, success=False, skipped=True, reason='cooldown', seconds_left=cooldown.seconds_left)
            return

    logger.info('--- Обработка: %s ---', target.name)
    with timed_phase('target_open_messages', target=target.name):
        open_messages(page)
    require_work_session(page)
    try:
        with timed_phase('target_open_chat', target=target.name, method='list'):
            chat_opened = open_chat_by_list(page, target.name)
    except Exception as exc:
        logger.warning('List strategy failed for %s; trying profile fallback: %s', target.name, exc)
        chat_opened = False
    if not chat_opened:
        logger.info('List strategy did not open chat for %s; trying profile fallback.', target.name)
        _append_run_history('target_open_chat_result', target=target.name, method='list', ok=False, reason='not_found')
    if not chat_opened and target.profile_url:
        try:
            with timed_phase('target_open_chat', target=target.name, method='profile'):
                chat_opened = open_chat_by_profile(page, target.profile_url)
        except Exception as exc:
            logger.error('Не удалось открыть чат через профиль для %s: %s', target.name, exc)
            summary.add_result(target=target.name, success=False, reason='profile_open_failed')
            _append_run_history('target_result', target=target.name, success=False, reason='profile_open_failed', error=str(exc))
            return
    elif not chat_opened and not target.profile_url:
        logger.error('Пропускаем %s: нет ссылки.', target.name)
        summary.add_result(target=target.name, success=False, reason='profile_url_missing')
        _append_run_history('target_result', target=target.name, success=False, reason='profile_url_missing')
        return

    require_work_session(page)
    runtime_state = control_store.load_state()
    message = message_selector.next()
    if runtime_state.dry_run:
        logger.info('DRY RUN: сообщение для %s не отправляется: %s', target.name, message)
        summary.add_result(target=target.name, success=True, message=message, streak_count=store.get_streak_count(target.name))
        _append_run_history('target_result', target=target.name, success=True, dry_run=True, reason='dry_run', message=message)
        return

    guard_ok, guard = acquire_target_send_guard(target.name)
    if not guard_ok:
        logger.warning('[%s] Send skipped by recent target guard: %s', target.name, guard)
        summary.add_result(target=target.name, success=False, skipped=True, reason='recent_guard')
        _append_run_history('target_result', target=target.name, success=False, skipped=True, reason='recent_guard', guard=guard)
        return

    try:
        with timed_phase('target_send_message', target=target.name):
            send_message(page, message)
    except Exception as exc:
        logger.error('Не удалось отправить сообщение для %s: %s', target.name, exc)
        summary.add_result(target=target.name, success=False, reason='send_failed')
        _append_run_history('target_result', target=target.name, success=False, reason='send_failed', error=str(exc))
        return

    with timed_phase('target_save_state', target=target.name):
        store.mark_sent_now(target.name)
        mark_target_guard_sent(target.name)
        streak = store.update_streak_stats(target.name)
    logger.info('✅ Успех! Текущий огонек: %s', streak.current_count)
    tg.send_target_success(target_name=target.name, message=message, streak_count=streak.current_count, is_new_day=streak.is_new_day)
    summary.add_result(target=target.name, success=True, message=message, streak_count=streak.current_count)
    _append_run_history('target_result', target=target.name, success=True, message=message, streak_count=streak.current_count, is_new_day=streak.is_new_day)
    time.sleep(1.5)


class AuthSessionUnavailable(RuntimeError):
    pass


def mark_session_import_required(reason: str) -> None:
    marker = PATHS.state_dir / 'session_import_required.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'reason': reason, 'at': time.time()}), encoding='utf-8')
    record_session_validation(PATHS.state_dir, valid=False, source='worker', reason=reason)
    logger.warning('Работа остановлена: нужен новый перенос входа из Chrome. Причина: %s', reason)


def require_work_session(page) -> None:
    if not is_logged_in(page) or detect_captcha(page, timeout_ms=700).get('found'):
        mark_session_import_required('session_lost_during_run')
        raise AuthSessionUnavailable('Сессия потеряна. Повторите перенос входа из Chrome.')


def open_logged_in_work_browser(playwright):
    # Unattended runs never reset profiles or open a password/QR login flow.
    if (PATHS.state_dir / 'session_import_required.json').exists():
        logger.warning('Нужен перенос входа из Chrome; повторная проверка браузером пропущена.')
        return False, None, None
    if auth_backoff_seconds_left(AUTH_BACKOFF_FILE) > 0:
        logger.warning('Пауза авторизации активна; браузер не открывается.')
        return False, None, None
    if browser_profile_needs_recovery(PATHS.user_data_dir):
        mark_session_import_required('profile_key_unreadable')
        return False, None, None
    context = None
    page = None
    keep = False
    try:
        with timed_phase('work_browser_launch'):
            context = init_work_browser(playwright, PATHS.user_data_dir)
        page = context.pages[0] if context.pages else context.new_page()
        with timed_phase('work_browser_goto_messages'):
            response = page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=60000)
        if response is not None and response.status >= 400:
            logger.warning('TikTok недоступен (HTTP %s); повторный вход не выполняется.', response.status)
            return False, None, None
        if detect_captcha(page, timeout_ms=700).get('found'):
            mark_session_import_required('verification_required')
            return False, None, None
        if not wait_for_authenticated_page(page):
            mark_session_import_required('session_not_confirmed')
            return False, None, None
        clear_auth_backoff(AUTH_BACKOFF_FILE)
        record_session_validation(PATHS.state_dir, valid=True, source='worker')
        keep = True
        return True, context, page
    finally:
        if context is not None and not keep:
            safe_close_context(context, page)


def run_browser_self_test() -> dict[str, Any]:
    result: dict[str, Any] = {
        'ok': False,
        'mode': 'self_test_no_send',
        'profile_name': ACTIVE_PROFILE,
        'user_data_dir': str(PATHS.user_data_dir),
        'opened_browser': False,
        'logged_in': False,
        'captcha': {'found': False, 'selector': ''},
        'url': '',
        'phases': [],
    }
    context = None
    page = None

    if (PATHS.state_dir / 'session_import_required.json').exists():
        result['error'] = 'Нужен свежий перенос входа из Chrome. Повторные обращения к TikTok остановлены.'
        return result

    @contextmanager
    def local_phase(name: str):
        started = time.perf_counter()
        item: dict[str, Any] = {'phase': name, 'ok': False}
        try:
            yield
            item['ok'] = True
        except Exception as exc:
            item['error'] = str(exc)[:300]
            raise
        finally:
            item['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
            result['phases'].append(item)

    try:
        with sync_playwright() as playwright:
            try:
                with local_phase('launch_work_browser'):
                    context = init_work_browser(playwright, PATHS.user_data_dir)
                    result['opened_browser'] = True
                page = context.pages[0] if context.pages else context.new_page()
                with local_phase('goto_messages'):
                    page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=60000)
                    result['url'] = getattr(page, 'url', '') or ''
                with local_phase('captcha_probe'):
                    result['captcha'] = detect_captcha(page, timeout_ms=700)
                with local_phase('auth_probe'):
                    result['logged_in'] = wait_for_authenticated_page(page)
                    result['url'] = getattr(page, 'url', '') or result.get('url', '')
            finally:
                safe_close_context(context, page)
        result['ok'] = bool(result['opened_browser']) and bool(result['logged_in']) and not bool(result['captcha'].get('found'))
        record_session_validation(PATHS.state_dir, valid=result['ok'], source='self_test', reason='' if result['ok'] else 'session_not_confirmed')
        if not result['ok']:
            mark_session_import_required('self_test_not_confirmed')
    except Exception as exc:  # noqa: BLE001
        result['ok'] = False
        result['error'] = str(exc)
    return result


def final_run_status(summary: RunSummary, preferred: str = 'idle') -> str:
    """Return a stable terminal status without losing an explicit failure or stop."""
    if preferred not in {'idle', 'starting', 'running'}:
        return preferred
    if summary.failed_count:
        return 'partial' if summary.success_count else 'failed'
    if summary.success_count:
        return 'success'
    if summary.skipped_count:
        return 'skipped'
    return 'idle'


def release_worker_run() -> None:
    try:
        if control_store.load_state().last_run_pid == os.getpid():
            control_store.update_state(last_run_pid=None, stop_requested=False)
    finally:
        run_lock.release()


def check_tiktok_streak() -> None:
    logger.info('=== Запуск TikTok (%s) ===', ACTIVE_PROFILE)
    log_lifecycle('worker_start', profile=ACTIVE_PROFILE, pid=os.getpid())
    if not run_lock.acquire():
        logger.warning('Профиль %s уже запущен. Пропускаю этот старт.', ACTIVE_PROFILE)
        log_lifecycle('worker_skip_lock_busy', profile=ACTIVE_PROFILE)
        tg.send_text(f'⚠️ Профиль {ACTIVE_PROFILE} уже запущен. Новый запуск пропущен.')
        return
    atexit.register(release_worker_run)

    runtime_state = control_store.load_state()
    if runtime_state.paused:
        logger.warning('Бот на паузе. Запуск для %s пропущен.', ACTIVE_PROFILE)
        log_lifecycle('worker_skip_paused', profile=ACTIVE_PROFILE)
        _write_run_state(status='paused')
        run_lock.release()
        return

    control_store.update_state(stop_requested=False, last_run_pid=os.getpid(), last_run_started_at=time.time())
    run_start = time.time()
    profiles = control_store.load_profiles()
    targets = [Target(name=item['name'], profile_url=item.get('url')) for item in profiles.get(ACTIVE_PROFILE, [])]
    summary = RunSummary(profile_name=ACTIVE_PROFILE, total_targets=len(targets))
    notify_run_finished = True
    terminal_status = 'idle'
    terminal_reason = ''
    _write_run_state(status='starting', total_targets=len(targets), current_target=None)
    _append_run_history('run_started', total_targets=len(targets), dry_run=runtime_state.dry_run)
    try:
        if not targets:
            terminal_status = 'skipped'
            terminal_reason = 'no_targets'
            logger.warning('В активном профиле %s нет адресатов. Запуск пропущен.', ACTIVE_PROFILE)
            log_lifecycle('worker_skip_no_targets', profile=ACTIVE_PROFILE)
            summary.add_result(target='__run__', success=False, skipped=True, reason='no_targets')
            _append_run_history('run_skipped', reason='no_targets')
            return
        ready_targets = ready_targets_for_run(targets, summary)
        if not ready_targets:
            terminal_status = 'skipped'
            terminal_reason = 'cooldown'
            notify_run_finished = False
            logger.info('No ready targets for %s. Browser and auth check skipped.', ACTIVE_PROFILE)
            log_lifecycle('worker_skip_all_cooldown', profile=ACTIVE_PROFILE, target_count=len(targets))
            _append_run_history('run_skipped', reason='cooldown', cooldown_count=len(targets))
            return
        with timed_phase('internet_check'):
            online = wait_for_internet()
        if not online:
            terminal_status = 'failed'
            terminal_reason = 'offline'
            log_lifecycle('worker_failed_offline', profile=ACTIVE_PROFILE)
            _append_run_history('run_failed', reason='offline')
            return
        with sync_playwright() as playwright:
            logged_in, work_context, work_page = open_logged_in_work_browser(playwright)
            if not logged_in:
                terminal_status = 'failed'
                terminal_reason = 'auth_failed'
                log_lifecycle('worker_failed_auth', profile=ACTIVE_PROFILE)
                summary.add_result(target='__run__', success=False, reason='auth_failed')
                _append_run_history('run_failed', reason='auth_failed')
                tg.send_text('Авторизация не подтверждена. Остановите worker и повторите перенос входа из Chrome в приложении.')
                return
            try:
                logger.info('Рабочий Chromium готов; продолжаем рассылку.')
                if work_context is None or work_page is None:
                    with timed_phase('work_browser_launch_reopen'):
                        work_context = init_work_browser(playwright, PATHS.user_data_dir)
                    work_page = work_context.pages[0] if work_context.pages else work_context.new_page()
                    with timed_phase('work_browser_goto_messages_reopen'):
                        work_page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=60000)
                    with timed_phase('work_browser_captcha_check_reopen'):
                        require_work_session(work_page)
                stop_reason = stop_or_pause_reason()
                if stop_reason:
                    terminal_status = 'stopped'
                    terminal_reason = stop_reason
                    logger.warning('Получен %s перед началом рассылки. Запуск остановлен.', stop_reason)
                    summary.add_result(target='__run__', success=False, skipped=True, reason=stop_reason)
                    _append_run_history('run_stopped', reason=stop_reason)
                    return
                send_start_message(ready_targets, dry_run=runtime_state.dry_run)
                log_lifecycle('dispatch_start', profile=ACTIVE_PROFILE, ready_targets=len(ready_targets), dry_run=runtime_state.dry_run)
                logger.info('Переход к циклу рассылки. targets=%s', len(targets))
                _write_run_state(status='running', current_target=None, total_targets=len(ready_targets))
                for target in ready_targets:
                    stop_reason = stop_or_pause_reason()
                    if stop_reason:
                        terminal_status = 'stopped'
                        terminal_reason = stop_reason
                        logger.warning('Получен %s. Останавливаю цикл перед следующим адресатом.', stop_reason)
                        summary.add_result(target='__run__', success=False, skipped=True, reason=stop_reason)
                        _append_run_history('run_stopped', reason=stop_reason)
                        break
                    process_target(work_page, target, summary)
                logger.info('Выход из check_tiktok_streak(). success_count=%s, targets=%s', summary.success_count, len(targets))
            finally:
                logger.info('Закрытие браузера.')
                safe_close_context(work_context, work_page)
    except AuthSessionUnavailable:
        terminal_status = 'failed'
        terminal_reason = 'session_import_required'
        _append_run_history('run_failed', reason=terminal_reason)
    except Exception as exc:
        terminal_status = 'failed'
        terminal_reason = 'unhandled_exception'
        _append_run_history('run_failed', reason=terminal_reason, error=str(exc)[:300])
        raise
    finally:
        summary.duration_seconds = time.time() - run_start
        terminal_status = final_run_status(summary, terminal_status)
        completed_at = time.time()
        save_run_summary(summary)
        _write_run_state(
            status=terminal_status,
            reason=terminal_reason or None,
            completed_at=completed_at,
            current_target=None,
            total_targets=summary.total_targets,
            summary=summary.to_dict(),
        )
        _append_run_history(
            'run_finished',
            status=terminal_status,
            reason=terminal_reason or None,
            success_count=summary.success_count,
            skipped_count=summary.skipped_count,
            failed_count=summary.failed_count,
            duration_seconds=round(summary.duration_seconds, 2),
        )
        if notify_run_finished:
            tg.send_run_finished(profile_name=summary.profile_name, success_count=summary.success_count, skipped_count=summary.skipped_count, failed_count=summary.failed_count, duration_text=format_duration(int(summary.duration_seconds)))
        log_lifecycle(
            'worker_finish',
            profile=ACTIVE_PROFILE,
            status=terminal_status,
            reason=terminal_reason or None,
            success=summary.success_count,
            skipped=summary.skipped_count,
            failed=summary.failed_count,
            duration_seconds=round(summary.duration_seconds, 2),
        )
        release_worker_run()


if __name__ == '__main__':
    check_tiktok_streak()

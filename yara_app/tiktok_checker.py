
from __future__ import annotations

import atexit
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import urllib.parse
import urllib.request
from dataclasses import dataclass
from playwright.sync_api import sync_playwright

from yara_app.auth_flow import attach_auth_debug_listeners, auth_backoff_seconds_left, clear_auth_backoff, dump_auth_state, get_latest_tiktok_page, handle_captcha, init_auth_browser, init_work_browser, interactive_auth, is_logged_in, safe_close_context
from yara_app.config import BOT_NAME, MessageSelector, TG_CHAT_IDS, TG_DISABLE_NOTIFICATIONS, TG_TOKEN, get_cli_profile
from yara_app.project_adapter import ProjectAdapter
from yara_app.ttbot.models import ControlStore, RunSummary, StateStore, Target
from yara_app.runtime_paths import RunLock, build_profile_paths, configure_logging, format_duration, init_auth_runtime, recover_browser_profile_after_reinstall
from yara_app.tiktok_messenger import open_chat_by_list, open_chat_by_profile, open_messages, send_message

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

    def send_text_chunks(self, text: str, max_len: int = 3500, *, chat_ids: list[str] | None = None) -> None:
        if len(text) <= max_len:
            self.send_text(text, chat_ids=chat_ids)
            return
        chunk: list[str] = []
        size = 0
        for line in text.splitlines(True):
            if size + len(line) > max_len and chunk:
                self.send_text(''.join(chunk).rstrip(), chat_ids=chat_ids)
                chunk = [line]
                size = len(line)
            else:
                chunk.append(line)
                size += len(line)
        if chunk:
            self.send_text(''.join(chunk).rstrip(), chat_ids=chat_ids)

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

    def send_document(self, doc_path: str | Path, caption: str | None = None, *, chat_ids: list[str] | None = None) -> None:
        if not self.enabled:
            return
        doc_path = Path(doc_path)
        if not doc_path.exists():
            return
        targets = chat_ids or self.chat_ids
        for chat_id in targets:
            with doc_path.open('rb') as doc_file:
                data = {'chat_id': chat_id}
                if caption:
                    data['caption'] = caption
                self._post('sendDocument', data=data, files={'document': doc_file}, timeout=30)

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
auth_runtime = init_auth_runtime()
run_lock = RunLock(PATHS.run_lock_file)
message_selector = MessageSelector(control_store.load_messages())
auth_coord_adapter = ProjectAdapter()
RUN_STATE_FILE = PATHS.artifacts_dir / 'run_state.json'
RUN_HISTORY_FILE = PATHS.artifacts_dir / 'run_history.jsonl'
AUTH_BACKOFF_FILE = PATHS.state_dir / 'auth_backoff.json'


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
    except Exception as exc:
        logger.warning('Не удалось записать run history: %s', exc)


def wait_for_internet() -> bool:
    for _ in range(5):
        try:
            urllib.request.urlopen('https://www.google.com', timeout=5).close()
            return True
        except Exception:
            time.sleep(3)
    logger.error('Нет интернета. Завершение.')
    return False


def apply_stealth_script(page) -> None:
    stealth_script = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """
    page.add_init_script(stealth_script)


def save_run_summary(summary: RunSummary) -> None:
    (PATHS.artifacts_dir / 'run_summary.json').write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')


def send_start_message(targets: list[Target], *, dry_run: bool) -> None:
    tg.send_run_started(bot_name=BOT_NAME, profile_name=ACTIVE_PROFILE, target_count=len(targets), dry_run=dry_run)


def stop_requested() -> bool:
    return control_store.load_state().stop_requested


def stop_or_pause_reason() -> str | None:
    state = control_store.load_state()
    if state.stop_requested:
        return 'stop_requested'
    if state.paused:
        return 'paused'
    return None


def is_paused() -> bool:
    return control_store.load_state().paused


def process_target(page, target: Target, summary: RunSummary) -> None:
    _write_run_state(status='running', current_target=target.name)
    logger.info('Начало обработки адресата: %s | profile=%s', target.name, target.profile_url)
    cooldown = store.get_cooldown_status(target.name)
    if not cooldown.allowed:
        passed_text = 'неизвестно' if cooldown.hours_passed is None else format_duration(int(cooldown.hours_passed * 3600))
        left_text = format_duration(cooldown.seconds_left)
        logger.info('[%s] Рано. Прошло %s. Осталось %s.', target.name, passed_text, left_text)
        summary.add_result(target=target.name, success=False, skipped=True, reason='cooldown')
        _append_run_history('target_result', target=target.name, success=False, skipped=True, reason='cooldown', seconds_left=cooldown.seconds_left)
        return

    logger.info('--- Обработка: %s ---', target.name)
    open_messages(page)
    chat_opened = open_chat_by_list(page, target.name)
    if not chat_opened and target.profile_url:
        try:
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

    runtime_state = control_store.load_state()
    message = message_selector.next()
    if runtime_state.dry_run:
        logger.info('DRY RUN: сообщение для %s не отправляется: %s', target.name, message)
        summary.add_result(target=target.name, success=True, message=message, streak_count=store.get_streak_count(target.name))
        _append_run_history('target_result', target=target.name, success=True, dry_run=True, reason='dry_run', message=message)
        return

    try:
        send_message(page, message)
    except Exception as exc:
        logger.error('Не удалось отправить сообщение для %s: %s', target.name, exc)
        summary.add_result(target=target.name, success=False, reason='send_failed')
        _append_run_history('target_result', target=target.name, success=False, reason='send_failed', error=str(exc))
        return

    store.mark_sent_now(target.name)
    streak = store.update_streak_stats(target.name)
    logger.info('✅ Успех! Текущий огонек: %s', streak.current_count)
    tg.send_target_success(target_name=target.name, message=message, streak_count=streak.current_count, is_new_day=streak.is_new_day)
    summary.add_result(target=target.name, success=True, message=message, streak_count=streak.current_count)
    _append_run_history('target_result', target=target.name, success=True, message=message, streak_count=streak.current_count, is_new_day=streak.is_new_day)
    time.sleep(1.5)


def open_logged_in_work_browser(playwright):
    try:
        recovered_profile = recover_browser_profile_after_reinstall(PATHS)
        if recovered_profile is not None:
            logger.warning('Recovered incompatible Chrome profile after Windows reinstall. Backup: %s', recovered_profile)
            tg.send_text('Local TikTok browser profile was reset because Windows could not decrypt the old Chrome cookies. Try logging in again once the TikTok cooldown is over.')
    except Exception as exc:
        logger.error('Failed to recover browser profile before auth: %s', exc)

    work_context = None
    work_page = None
    keep_work_context = False
    try:
        logger.info('Открытие TikTok для проверки авторизации...')
        work_context = init_work_browser(playwright, PATHS.user_data_dir)
        work_page = work_context.pages[0] if work_context.pages else work_context.new_page()
        apply_stealth_script(work_page)
        work_page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=60000)
        handle_captcha(work_page, tg, PATHS.artifacts_dir)
        if is_logged_in(work_page):
            clear_auth_backoff(AUTH_BACKOFF_FILE)
            logger.info('Авторизация подтверждена; используем уже открытый рабочий Chromium.')
            keep_work_context = True
            return True, work_context, work_page
    finally:
        if work_context is not None and not keep_work_context:
            safe_close_context(work_context, work_page)

    auth_backoff_left = auth_backoff_seconds_left(AUTH_BACKOFF_FILE)
    if auth_backoff_left > 0:
        logger.warning('TikTok auth is temporarily rate-limited. Wait left: %s.', format_duration(auth_backoff_left))
        tg.send_text(f'TikTok still reports too many login attempts. Wait about {format_duration(auth_backoff_left)} before trying again.')
        return False, None, None

    logger.warning('🛑 НУЖЕН ВХОД! Открываю оконный режим...')
    control_bot_suspended = _suspend_control_bot_for_auth()
    auth_context = None
    auth_page = None
    try:
        auth_context = init_auth_browser(playwright, PATHS.user_data_dir)
        auth_page = auth_context.pages[0] if auth_context.pages else auth_context.new_page()
        attach_auth_debug_listeners(auth_context, auth_runtime)
        auth_page = get_latest_tiktok_page(auth_context, auth_page)
        dump_auth_state(auth_page, 'AUTH_BROWSER_STARTED')
        auth_page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=60000)
        auth_page = get_latest_tiktok_page(auth_context, auth_page)
        dump_auth_state(auth_page, 'AFTER_MESSAGES_GOTO')
        handle_captcha(auth_page, tg, PATHS.artifacts_dir)
        if not interactive_auth(auth_page, tg, auth_runtime, PATHS.artifacts_dir, AUTH_BACKOFF_FILE):
            return False, None, None
        auth_page = get_latest_tiktok_page(auth_context, auth_page)
        dump_auth_state(auth_page, 'AFTER_INTERACTIVE_AUTH')
        logger.info('После interactive_auth: url=%s, logged_in=%s', auth_page.url, is_logged_in(auth_page))
        logged_in = is_logged_in(auth_page)
        if logged_in:
            clear_auth_backoff(AUTH_BACKOFF_FILE)
        return logged_in, None, None
    finally:
        safe_close_context(auth_context, auth_page)
        _resume_control_bot_after_auth(control_bot_suspended)


def ensure_logged_in(playwright) -> bool:
    logged_in, work_context, work_page = open_logged_in_work_browser(playwright)
    safe_close_context(work_context, work_page)
    return logged_in


def check_tiktok_streak() -> None:
    logger.info('=== Запуск TikTok (%s) ===', ACTIVE_PROFILE)
    if not run_lock.acquire():
        logger.warning('Профиль %s уже запущен. Пропускаю этот старт.', ACTIVE_PROFILE)
        tg.send_text(f'⚠️ Профиль {ACTIVE_PROFILE} уже запущен. Новый запуск пропущен.')
        return
    atexit.register(run_lock.release)

    runtime_state = control_store.load_state()
    if runtime_state.paused:
        logger.warning('Бот на паузе. Запуск для %s пропущен.', ACTIVE_PROFILE)
        _write_run_state(status='paused')
        run_lock.release()
        return

    control_store.update_state(stop_requested=False, last_run_pid=os.getpid(), last_run_started_at=time.time())
    run_start = time.time()
    profiles = control_store.load_profiles()
    targets = [Target(name=item['name'], profile_url=item.get('url')) for item in profiles.get(ACTIVE_PROFILE, [])]
    summary = RunSummary(profile_name=ACTIVE_PROFILE, total_targets=len(targets))
    _write_run_state(status='starting', total_targets=len(targets), current_target=None)
    _append_run_history('run_started', total_targets=len(targets), dry_run=runtime_state.dry_run)
    try:
        if not targets:
            logger.warning('В активном профиле %s нет адресатов. Запуск пропущен.', ACTIVE_PROFILE)
            summary.add_result(target='__run__', success=False, skipped=True, reason='no_targets')
            _write_run_state(status='idle', current_target=None, summary=summary.to_dict())
            _append_run_history('run_skipped', reason='no_targets')
            return
        if not wait_for_internet():
            _write_run_state(status='offline')
            _append_run_history('run_failed', reason='offline')
            return
        with sync_playwright() as playwright:
            logged_in, work_context, work_page = open_logged_in_work_browser(playwright)
            if not logged_in:
                summary.add_result(target='__run__', success=False, reason='auth_failed')
                _write_run_state(status='auth_failed')
                _append_run_history('run_failed', reason='auth_failed')
                tg.send_text('❌ Авторизация не подтверждена. Проверь окно браузера и повтори запуск.')
                return
            try:
                logger.info('Рабочий Chromium готов; продолжаем рассылку.')
                if work_context is None or work_page is None:
                    work_context = init_work_browser(playwright, PATHS.user_data_dir)
                    work_page = work_context.pages[0] if work_context.pages else work_context.new_page()
                    apply_stealth_script(work_page)
                    work_page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=60000)
                    handle_captcha(work_page, tg, PATHS.artifacts_dir)
                stop_reason = stop_or_pause_reason()
                if stop_reason:
                    logger.warning('Получен %s перед началом рассылки. Запуск остановлен.', stop_reason)
                    summary.add_result(target='__run__', success=False, skipped=True, reason=stop_reason)
                    _append_run_history('run_stopped', reason=stop_reason)
                    return
                send_start_message(targets, dry_run=runtime_state.dry_run)
                logger.info('Переход к циклу рассылки. targets=%s', len(targets))
                _write_run_state(status='running', current_target=None, total_targets=len(targets))
                for target in targets:
                    stop_reason = stop_or_pause_reason()
                    if stop_reason:
                        logger.warning('Получен %s. Останавливаю цикл перед следующим адресатом.', stop_reason)
                        summary.add_result(target='__run__', success=False, skipped=True, reason=stop_reason)
                        _append_run_history('run_stopped', reason=stop_reason)
                        break
                    process_target(work_page, target, summary)
                logger.info('Выход из check_tiktok_streak(). success_count=%s, targets=%s', summary.success_count, len(targets))
            finally:
                logger.info('Закрытие браузера.')
                safe_close_context(work_context, work_page)
    finally:
        summary.duration_seconds = time.time() - run_start
        save_run_summary(summary)
        _write_run_state(status='idle', current_target=None, summary=summary.to_dict())
        _append_run_history('run_finished', success_count=summary.success_count, skipped_count=summary.skipped_count, failed_count=summary.failed_count, duration_seconds=round(summary.duration_seconds, 2))
        tg.send_run_finished(profile_name=summary.profile_name, success_count=summary.success_count, skipped_count=summary.skipped_count, failed_count=summary.failed_count, duration_text=format_duration(int(summary.duration_seconds)))
        control_store.update_state(stop_requested=False)
        run_lock.release()


if __name__ == '__main__':
    check_tiktok_streak()

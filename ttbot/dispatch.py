from __future__ import annotations

import atexit
import datetime
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

import urllib.request
from playwright.sync_api import sync_playwright

from auth_flow import (
    attach_auth_debug_listeners,
    block_heavy_content,
    dump_auth_state,
    get_latest_tiktok_page,
    handle_captcha,
    init_auth_browser,
    init_work_browser,
    interactive_auth,
    is_logged_in,
)
from config import MessageSelector
from ttbot.models import ChatOpenResult, RunSummary, Target, TargetResult
from tiktok_messenger import ChatOpener, open_messages, send_heart


@dataclass
class TikTokSession:
    """Thin wrapper around the current Playwright page.

    The session centralises all page-level actions used by the dispatch service
    while preserving the historically working selectors and behaviour.
    """

    page: object
    chat_opener: ChatOpener
    artifacts_dir: Path
    telegram: object

    def refresh_page_reference(self) -> object:
        context = getattr(self.page, 'context', None)
        if context is None:
            return self.page
        self.page = get_latest_tiktok_page(context, self.page)
        return self.page

    def ensure_logged_in(self) -> bool:
        self.refresh_page_reference()
        return is_logged_in(self.page)

    def handle_captcha_if_needed(self) -> bool:
        self.refresh_page_reference()
        return handle_captcha(self.page, self.telegram, self.artifacts_dir)

    def open_chat(self, target: Target) -> ChatOpenResult:
        """Restore the first-version flow before opening a specific chat.

        The original project re-opened the inbox page for every target before it
        attempted Plan A. That behaviour was lost in the service-layer refactor,
        which could leave the session on a profile/chat substate and make the
        left inbox list less reliable. We restore that exact step here.
        """

        self.refresh_page_reference()
        open_messages(self.page)
        self.refresh_page_reference()
        return self.chat_opener.open_chat(self.page, target.name, target.profile_url)

    def send_symbol(self, symbol: str) -> None:
        self.refresh_page_reference()
        send_heart(self.page, symbol)


TIKTOK_MESSAGES_URL = 'https://www.tiktok.com/messages'
INTERNET_CHECK_URL = 'https://www.google.com'
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


@dataclass
class BrowserResources:
    browser: object | None = None
    page: object | None = None


class DispatchService:
    """Run the full TikTok workflow for one active profile."""

    def __init__(self, runtime, settings, profile_config) -> None:
        self.runtime = runtime
        self.settings = settings
        self.profile_config = profile_config
        self.logger = logging.getLogger(__name__)
        self.chat_opener = ChatOpener()
        self.message_selector = MessageSelector(list(self.settings.messages))

    def run(self) -> RunSummary:
        summary = RunSummary(
            profile_name=self.profile_config.name,
            total_targets=len(self.profile_config.targets),
        )

        self.logger.info('=== Запуск TikTok (%s) ===', self.profile_config.name)
        if not self.runtime.run_lock.acquire():
            self.logger.warning('Профиль %s уже запущен. Пропускаю этот старт.', self.profile_config.name)
            self.runtime.telegram.send_text(
                f'⚠️ Профиль {self.profile_config.name} уже запущен. Новый запуск пропущен.'
            )
            return summary

        atexit.register(self.runtime.run_lock.release)
        run_started_at = time.time()

        try:
            if not self.wait_for_internet():
                return summary

            self.send_start_message(self.profile_config.targets)
            for attempt in range(1, self.settings.dispatch.retry_attempts + 1):
                resources = BrowserResources()
                try:
                    with sync_playwright() as playwright:
                        resources = self.open_work_browser(playwright, attempt)
                        session = TikTokSession(
                            page=resources.page,
                            chat_opener=self.chat_opener,
                            artifacts_dir=self.runtime.artifacts_dir,
                            telegram=self.runtime.telegram,
                        )
                        session.handle_captcha_if_needed()

                        if not session.ensure_logged_in():
                            self.logger.warning('🛑 НУЖЕН ВХОД! Открываю оконный режим...')
                            self.close_browser(resources)
                            resources = BrowserResources()
                            resources = self.open_authenticated_browser(playwright)
                            session = TikTokSession(
                                page=resources.page,
                                chat_opener=self.chat_opener,
                                artifacts_dir=self.runtime.artifacts_dir,
                                telegram=self.runtime.telegram,
                            )

                        self.logger.info('Переход к циклу рассылки. targets=%s', len(self.profile_config.targets))
                        for target in self.profile_config.targets:
                            result = self.process_target(session, target)
                            summary.add(result)

                        summary.duration_seconds = time.time() - run_started_at
                        self.write_run_summary(summary)
                        self.send_completion_message(summary)
                        self.logger.info(
                            'Выход из check_tiktok_streak(). success_count=%s, targets=%s',
                            summary.success_count,
                            summary.total_targets,
                        )
                        time.sleep(self.settings.dispatch.final_delay_seconds)
                        return summary
                except Exception as exc:
                    self.logger.error('❌ Ошибка на попытке %s: %s', attempt, exc)
                    if resources.page and attempt == self.settings.dispatch.retry_attempts:
                        self.capture_failure(resources.page, 'error_report.png')
                    if attempt < self.settings.dispatch.retry_attempts:
                        self.logger.info('🔄 Ждем %s секунд перед следующей попыткой...', self.settings.dispatch.retry_delay_seconds)
                        time.sleep(self.settings.dispatch.retry_delay_seconds)
                    else:
                        self.runtime.telegram.send_text(
                            f'❌ Скрипт упал {self.settings.dispatch.retry_attempts} раза подряд. Последняя ошибка: {exc}'
                        )
                finally:
                    self.close_browser(resources)
        finally:
            summary.duration_seconds = time.time() - run_started_at
            self.write_run_summary(summary)
            self.runtime.run_lock.release()

        return summary

    def wait_for_internet(self) -> bool:
        for _ in range(self.settings.dispatch.internet_check_attempts):
            try:
                urllib.request.urlopen(INTERNET_CHECK_URL, timeout=5).close()
                return True
            except Exception:
                time.sleep(self.settings.dispatch.internet_check_delay_seconds)
        self.logger.error('Нет интернета. Завершение.')
        return False

    def send_start_message(self, targets: list[Target] | tuple[Target, ...]) -> None:
        hour = datetime.datetime.now().hour
        if 5 <= hour < 12:
            greeting = '🌅 Доброе утро!'
        elif 12 <= hour < 18:
            greeting = '☀️ Добрый день!'
        elif 18 <= hour < 23:
            greeting = '🌇 Добрый вечер!'
        else:
            greeting = '🌙 Добрая ночь!'

        chatty_phrases = [
            'Разминаю шестеренки...',
            'Надеваю костюм ниндзя...',
            'Погнали! 🚀',
            'Пью виртуальный кофе ☕️',
        ]
        suffix = ' [DRY RUN]' if self.settings.dispatch.dry_run else ''
        self.runtime.telegram.send_text(
            f"{greeting}\n🤖 tiktok_heart_bot ({self.profile_config.name}) проснулся!{suffix}\n"
            f"{random.choice(chatty_phrases)}\nДрузей в списке: {len(targets)} 🕵️‍♂️"
        )

    def open_work_browser(self, playwright, attempt: int) -> BrowserResources:
        browser = init_work_browser(playwright, self.runtime.paths.user_data_dir)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.add_init_script(STEALTH_SCRIPT)
        page.route('**/*', lambda route: block_heavy_content(route, self.runtime.policy.block_media))
        self.logger.info('Открытие TikTok (Попытка %s/%s)...', attempt, self.settings.dispatch.retry_attempts)
        page.goto(TIKTOK_MESSAGES_URL, wait_until='domcontentloaded', timeout=60000)
        return BrowserResources(browser=browser, page=page)

    def open_authenticated_browser(self, playwright) -> BrowserResources:
        browser = init_auth_browser(playwright, self.runtime.paths.user_data_dir)
        page = browser.pages[0] if browser.pages else browser.new_page()
        attach_auth_debug_listeners(browser, self.runtime.auth_runtime)
        dump_auth_state(page, 'AUTH_BROWSER_STARTED')
        page.goto(TIKTOK_MESSAGES_URL, wait_until='domcontentloaded')
        dump_auth_state(page, 'AFTER_MESSAGES_GOTO')

        previous_block_media = self.runtime.policy.block_media
        self.runtime.policy.block_media = False
        try:
            interactive_auth(page, self.runtime.telegram, self.runtime.auth_runtime, self.runtime.artifacts_dir)
        finally:
            self.runtime.policy.block_media = previous_block_media

        dump_auth_state(page, 'AFTER_INTERACTIVE_AUTH')
        return BrowserResources(browser=browser, page=page)

    def process_target(self, session: TikTokSession, target: Target) -> TargetResult:
        self.logger.info('Начало обработки адресата: %s | profile=%s', target.name, target.profile_url)
        cooldown = self.runtime.store.get_cooldown_status(target.name)
        if not cooldown.allowed:
            self.logger.info(
                '[%s] Рано. Прошло %s. Осталось %s.',
                target.name,
                self.runtime.store.describe_hours_passed(cooldown.hours_passed),
                self.runtime.store.format_duration(cooldown.seconds_left),
            )
            return TargetResult(target=target, success=False, reason='cooldown')

        self.logger.info('--- Обработка: %s ---', target.name)
        open_result = session.open_chat(target)
        if not open_result.ok:
            screenshot_path = self.capture_failure(session.page, f'chat_open_{self.runtime.store.safe_filename(target.name)}.png')
            return TargetResult(
                target=target,
                success=False,
                reason=open_result.reason or 'chat_open_failed',
                screenshot_path=screenshot_path,
            )

        message = self.message_selector.next()
        if self.settings.dispatch.dry_run:
            self.logger.info('[DRY RUN] Пропускаю отправку сообщения: %s', message)
            return TargetResult(
                target=target,
                success=True,
                message=message,
                streak_count=self.runtime.store.update_streak_stats(target.name).current_count,
                reason='dry_run',
                chat_method=open_result.method,
            )

        try:
            session.send_symbol(message)
        except Exception as exc:
            self.logger.error('Не удалось отправить сообщение для %s: %s', target.name, exc)
            screenshot_path = self.capture_failure(session.page, f'send_{self.runtime.store.safe_filename(target.name)}.png')
            return TargetResult(
                target=target,
                success=False,
                reason='send_failed',
                screenshot_path=screenshot_path,
                chat_method=open_result.method,
            )

        self.runtime.store.mark_sent_now(target.name)
        streak = self.runtime.store.update_streak_stats(target.name)
        self.logger.info('✅ Успех! Текущий огонек: %s', streak.current_count)
        self.runtime.telegram.send_text(
            f'✅ {target.name}: {message} | streak={streak.current_count}'
        )
        time.sleep(self.settings.dispatch.post_send_delay_seconds)
        return TargetResult(
            target=target,
            success=True,
            message=message,
            streak_count=streak.current_count,
            is_new_day=streak.is_new_day,
            chat_method=open_result.method,
        )

    def capture_failure(self, page, file_name: str) -> Path | None:
        if page is None:
            return None
        screenshot_path = self.runtime.artifacts_dir / file_name
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            self.runtime.telegram.send_photo(screenshot_path)
            return screenshot_path
        except Exception as exc:
            self.logger.warning('Не удалось снять скриншот %s: %s', file_name, exc)
            return None

    def write_run_summary(self, summary: RunSummary) -> Path:
        payload = json.dumps(summary.to_dict(), ensure_ascii=False, indent=2)
        summary_path = self.runtime.artifacts_dir / 'run_summary.json'
        summary_path.write_text(payload, encoding='utf-8')
        return summary_path

    def send_completion_message(self, summary: RunSummary) -> None:
        self.runtime.telegram.send_text(
            '✅ Запуск завершен '\
            f'({summary.profile_name}): ok={summary.success_count}, skip={summary.skipped_count}, fail={summary.failed_count}'
        )

    def _collect_context_pages(self, resources: BrowserResources) -> list[object]:
        browser = resources.browser
        if browser is None:
            return []
        pages = list(getattr(browser, 'pages', []) or [])
        if resources.page is not None and resources.page not in pages:
            pages.append(resources.page)
        return pages

    def close_browser(self, resources: BrowserResources) -> None:
        context = resources.browser
        if not context:
            return

        pages = self._collect_context_pages(resources)
        for page in pages:
            try:
                page.unroute('**/*')
            except Exception:
                pass

        # Give Playwright a tiny moment to drain in-flight routed requests before
        # pages/context are torn down. This avoids noisy route-related shutdown errors.
        time.sleep(0.05)

        for page in reversed(pages):
            try:
                page.close()
            except Exception:
                pass

        self.logger.info('Закрытие браузера.')
        try:
            context.close()
        except Exception as exc:
            self.logger.warning('Контекст браузера уже закрыт или завершился с ошибкой: %s', exc)
        finally:
            resources.page = None
            resources.browser = None

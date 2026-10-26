"""Short-lived login sessions; Playwright stays on its owning thread."""
from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from yara_app.fileio import atomic_write_text, read_json
from yara_app.native_login import (
    LOGIN_PENDING,
    browser_lease,
    chrome_profile_running,
    mark_google_chrome,
    uses_google_chrome,
)

logger = logging.getLogger(__name__)
LOGIN_URL = 'https://www.tiktok.com/login?lang=en'
MESSAGES_URL = 'https://www.tiktok.com/messages'
QR_SELECTOR = '[data-e2e="qr-code"] canvas:visible, canvas:visible, [data-e2e="qr-code"] img:visible'


@dataclass
class LoginSession:
    id: str
    profile: str
    owner: int
    method: str
    stage: str = 'starting'
    inbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=4), repr=False)
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)


class LoginPage:
    """UI actions only. Never logs input, responses, cookies or signed URLs."""
    def __init__(self, page):
        self.page = page

    def click_text(self, pattern: str, *, required: bool = False) -> bool:
        locator = self.page.get_by_text(re.compile(pattern, re.I)).filter(visible=True)
        try:
            locator.first.wait_for(state='visible', timeout=10000 if required else 1500)
        except Exception:
            if required:
                if self.authenticated():
                    return False
                raise RuntimeError('Не удалось открыть способ входа. Попробуйте вход на компьютере.') from None
            return False
        if locator.count():
            locator.first.click(timeout=5000)
            return True
        if required:
            raise RuntimeError('Не удалось открыть способ входа. Попробуйте вход на компьютере.')
        return False

    def open(self, method: str) -> None:
        self.page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=30000)
        # TikTok paints these choices before registering their click handlers.
        self.page.wait_for_timeout(1600)
        if self.authenticated():
            return
        if method == 'qr':
            if not self.click_text(r'^(Use QR code|Использовать QR-код|Вход по QR-коду|QR-код)$', required=True):
                return
            self.page.locator(QR_SELECTOR).first.wait_for(
                state='visible', timeout=12000)
            self.page.wait_for_timeout(500)
        else:
            if not self.click_text(r'^(Use phone / email / username|Использовать телефон / почту / имя пользователя|Телефон / почта / имя пользователя)$', required=True):
                return
            self.page.wait_for_timeout(500)
            self.click_text(r'^(Log in with email or username|Войти по почте или имени пользователя|Почта / имя пользователя)$')
            self.click_text(r'^(Log in with password|Войти с паролем)$')
            self.page.locator('input[name="username"]:visible').wait_for(state='visible', timeout=8000)
            self.page.locator('input[type="password"]:visible').wait_for(state='visible', timeout=8000)

    def qr_photo(self, path: Path) -> None:
        locator = self.page.locator(QR_SELECTOR)
        if not locator.count():
            raise RuntimeError('QR-код не появился. Попробуйте вход по паролю или на компьютере.')
        locator.first.screenshot(path=str(path), type='png', timeout=4000)

    def password(self, identifier: str, password: str) -> None:
        self.page.locator('input[name="username"]:visible').fill(identifier.removeprefix('@'), timeout=4000)
        self.page.locator('input[type="password"]:visible').fill(password, timeout=4000)
        self.page.get_by_role('button', name=re.compile(r'^(Log in|Войти)$', re.I)).click(timeout=5000)

    def code_fields(self):
        fields = self.page.locator('input[autocomplete="one-time-code"]:visible, input[name*="code" i]:visible, '
            'input[placeholder*="code" i]:visible, input[placeholder*="код" i]:visible')
        if fields.count():
            return fields
        boxes = self.page.locator('input[maxlength="1"][inputmode="numeric"]:visible')
        return boxes if 4 <= boxes.count() <= 10 else None

    def code_request_available(self) -> bool:
        return self.page.get_by_role('button', name=re.compile(r'^(Send code|Отправить код|Получить код)$', re.I)).filter(visible=True).count() == 1

    def request_code(self) -> bool:
        button = self.page.get_by_role('button', name=re.compile(r'^(Send code|Отправить код|Получить код)$', re.I)).filter(visible=True)
        if button.count() != 1 or not button.is_enabled():
            return False
        button.click(timeout=4000)
        return True

    def code(self, value: str) -> None:
        fields = self.code_fields()
        if fields is None:
            raise RuntimeError('Поле кода уже закрыто. Проверьте сообщение о входе.')
        if fields.count() == 1:
            fields.first.fill(value, timeout=4000)
        elif fields.count() == len(value):
            for index, digit in enumerate(value):
                fields.nth(index).fill(digit, timeout=4000)
        else:
            raise RuntimeError('Код не подходит по длине. Начните вход заново.')
        button = self.page.get_by_role('button', name=re.compile(r'^(Verify|Confirm|Continue|Log in|Подтвердить|Продолжить|Войти)$', re.I)).filter(visible=True)
        if button.count() == 1 and button.is_enabled():
            button.click(timeout=4000)

    def authenticated(self) -> bool:
        from yara_app.auth_flow import is_logged_in
        return is_logged_in(self.page)

    def challenge(self) -> bool:
        return bool(self.page.locator('#captcha-verify-image:visible, .captcha-disable-scroll:visible, '
            'iframe[src*="captcha"]:visible, #arkose-iframe:visible').count())

    def rejected(self) -> bool:
        return bool(self.page.locator('[role="alert"]:visible, [data-e2e="error-message"]:visible').filter(
            has_text=re.compile(r'incorrect|invalid|too many|неверн|неправильн|слишком много', re.I)).count())

    def confirmation_photo(self, path: Path) -> Path | None:
        try:
            self.page.screenshot(path=str(path), type='png', full_page=False, timeout=3000,
                                 mask=[frame.locator('input') for frame in self.page.frames])
            return path
        except Exception:
            return None


class InteractiveLogin:
    def __init__(self, adapter, emit, allowed, *, channel: str = 'telegram', timeout_seconds: float = 600):
        self.adapter = adapter
        self.emit = emit
        self.allowed = allowed
        self.channel = channel
        self.timeout_seconds = min(600, timeout_seconds)
        self.lock = threading.RLock()
        self.sessions: dict[str, LoginSession] = {}

    def current(self, owner: int) -> LoginSession | None:
        with self.lock:
            return next((s for s in self.sessions.values() if s.owner == owner and not s.stop.is_set()), None)

    def for_profile(self, profile: str) -> LoginSession | None:
        with self.lock:
            return next((s for s in self.sessions.values() if s.profile == profile), None)

    def start(self, profile: str, owner: int, method: str) -> LoginSession:
        if method not in {'qr', 'password'} or not self.allowed(owner, profile):
            raise PermissionError('Этот вход вам недоступен.')
        self.adapter._bot_profile_root(profile)  # Validate before starting a thread.
        if self.adapter.get_worker_status().running:
            raise ValueError('Сначала остановите отправку.')
        with self.lock:
            if self.sessions:
                raise ValueError('Сейчас уже выполняется вход. Завершите или отмените его.')
            session = LoginSession(secrets.token_hex(8), profile, owner, method)
            self.sessions[session.id] = session
            session.thread = threading.Thread(target=self._run, args=(session,), daemon=True)
            session.thread.start()
            return session

    def command(self, owner: int, sid: str, action: str) -> None:
        with self.lock:
            session = self.sessions.get(sid)
            if not session or session.owner != owner or not self.allowed(owner, session.profile):
                raise PermissionError('Этот вход уже закрыт или вам недоступен.')
            if action == 'cancel':
                session.stop.set()
            elif action in {'check', 'refresh', 'request_code'}:
                if action == 'request_code' and session.stage not in {'code', 'code_request'}:
                    raise ValueError('Сейчас код не требуется.')
                if action == 'refresh' and (session.method != 'qr' or session.stage != 'qr'):
                    raise ValueError('Дождитесь QR-кода.')
                if session.inbox.full():
                    raise ValueError('Подождите, предыдущий шаг ещё выполняется.')
                if action in {'request_code', 'refresh'}:
                    session.stage = 'busy'
                session.inbox.put_nowait((action, ''))
            else:
                raise ValueError('Кнопка устарела. Начните вход заново.')

    def submit(self, owner: int, text: str) -> bool:
        session = self.current(owner)
        if session is None:
            return False
        if not self.allowed(owner, session.profile):
            session.stop.set()
            raise PermissionError('Доступ к профилю отозван.')
        stage = session.stage
        if stage not in {'identifier', 'password', 'code'}:
            raise ValueError('Дождитесь следующего шага входа или нажмите «Отмена».')
        if stage == 'identifier' and (not text.strip() or len(text) > 120):
            raise ValueError('Пришлите логин или почту TikTok.')
        if stage == 'password' and not 1 <= len(text) <= 200:
            raise ValueError('Пришлите пароль одним сообщением.')
        if stage == 'code' and not re.fullmatch(r'\d{4,10}', text.strip()):
            raise ValueError('Пришлите код из цифр одним сообщением.')
        with self.lock:
            if session.inbox.full() or session.stage != stage:
                raise ValueError('Предыдущий шаг ещё выполняется. Подождите.')
            session.stage = 'busy'
            session.inbox.put_nowait((stage, text if stage == 'password' else text.strip()))
        return True

    def cancel(self, owner: int) -> None:
        session = self.current(owner)
        if session:
            session.stop.set()
            if session.thread and session.thread is not threading.current_thread():
                session.thread.join(timeout=5)

    def close(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop.set()
        for session in sessions:
            if session.thread:
                session.thread.join(timeout=5)

    def _emit(self, session: LoginSession, stage: str, text: str, photo: Path | None = None) -> None:
        if not self.allowed(session.owner, session.profile):
            session.stop.set()
            return
        with self.lock:
            session.stage = stage
        self.emit(session, text, photo)

    def _run(self, session: LoginSession) -> None:
        from playwright.sync_api import sync_playwright

        from yara_app.auth_flow import (
            _launch_persistent_chromium,
            clear_auth_backoff,
            record_session_validation,
            wait_for_authenticated_page,
        )
        root = self.adapter._bot_profile_root(session.profile)
        browser = root / 'browser'
        pending = browser / LOGIN_PENDING
        identifier = ''
        value = ''
        success = False
        committed = False
        mode = ''
        photo = None
        terminal_stage = 'failed'
        terminal_text = 'Не получилось войти. Попробуйте ещё раз.'
        try:
            with browser_lease(browser):
                if not self.allowed(session.owner, session.profile):
                    raise PermissionError('Доступ к профилю отозван.')
                if self.adapter.get_worker_status().running:
                    raise RuntimeError('Сначала остановите отправку.')
                active = browser / 'user_data'
                mode = 'active' if uses_google_chrome(active) else 'staged'
                target = active if mode == 'active' else browser / 'dedicated_login' / 'user_data'
                if chrome_profile_running(target):
                    raise RuntimeError('Закройте окно входа на компьютере и попробуйте снова.')
                if pending.exists():
                    previous = read_json(pending, {}) or {}
                    # An abandoned in-app/Telegram flow has no browser lease.
                    # A fresh explicit login can resume the same saved profile.
                    if previous.get('flow') not in {'telegram', 'desktop'}:
                        raise RuntimeError('Завершите или отмените предыдущий вход на компьютере.')
                if mode == 'active':
                    self.adapter.create_auth_backup(session.profile)
                mark_google_chrome(target)
                atomic_write_text(pending, json.dumps({'mode': mode, 'flow': self.channel, 'id': session.id}))
                with sync_playwright() as runtime:
                    context = _launch_persistent_chromium(runtime, target, headless=True, _acquire_lease=False)
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.set_default_timeout(4000)
                        page.goto(MESSAGES_URL, wait_until='domcontentloaded', timeout=30000)
                        view = LoginPage(page)
                        success = wait_for_authenticated_page(page, timeout_ms=10000)
                        if not success:
                            view.open(session.method)
                            success = view.authenticated()
                            if view.challenge():
                                raise RuntimeError('TikTok просит проверку на картинке. Продолжите вход на компьютере.')
                            photo = root / 'artifacts' / f'login_qr_{session.id}.png'
                            photo.parent.mkdir(parents=True, exist_ok=True)
                            if not success and session.method == 'qr':
                                view.qr_photo(photo)
                                self._emit(session, 'qr', 'Откройте TikTok на телефоне, отсканируйте QR и подтвердите вход.', photo)
                            elif not success:
                                self._emit(session, 'identifier', 'Пришлите логин или почту TikTok.')
                            deadline = time.monotonic() + self.timeout_seconds
                            code_requested = False
                            while not success and not session.stop.is_set() and time.monotonic() < deadline:
                                if not self.allowed(session.owner, session.profile):
                                    break
                                if view.authenticated():
                                    page.goto(MESSAGES_URL, wait_until='domcontentloaded', timeout=30000)
                                    success = wait_for_authenticated_page(page, timeout_ms=10000)
                                    break
                                if view.challenge():
                                    raise RuntimeError('TikTok просит проверку на картинке. Продолжите вход на компьютере.')
                                if session.stage == 'waiting':
                                    code_visible = view.code_fields() is not None
                                    if code_visible and not code_requested:
                                        code_requested = True
                                        self._emit(session, 'code', 'TikTok запросил код. Пришлите его сюда одним сообщением.', view.confirmation_photo(photo))
                                    elif not code_visible:
                                        code_requested = False
                                        if view.code_request_available():
                                            self._emit(session, 'code_request', 'Нажмите «Получить код», затем пришлите код сюда.')
                                if session.stage == 'waiting' and view.rejected():
                                    raise RuntimeError('TikTok не принял вход. Проверьте данные и попробуйте снова.')
                                try:
                                    action, value = session.inbox.get(timeout=0.8)
                                except queue.Empty:
                                    continue
                                try:
                                    if session.stop.is_set() or not self.allowed(session.owner, session.profile):
                                        break
                                    if action == 'identifier':
                                        identifier = value
                                        self._emit(session, 'password', 'Теперь пришлите пароль. Он не сохраняется; сообщение с паролем попробуем удалить.')
                                    elif action == 'password':
                                        view.password(identifier, value)
                                        identifier = ''
                                        self._emit(session, 'waiting', 'Проверяем вход. Если TikTok попросит подтвердить его на телефоне, подтвердите.')
                                    elif action == 'code':
                                        view.code(value)
                                        self._emit(session, 'waiting', 'Проверяем код…')
                                    elif action == 'request_code':
                                        view.request_code()
                                        self._emit(session, 'code', 'Пришлите код из SMS или письма.', view.confirmation_photo(photo))
                                    elif action == 'refresh' and session.method == 'qr':
                                        view.open('qr')
                                        view.qr_photo(photo)
                                        self._emit(session, 'qr', 'Новый QR-код. Отсканируйте его в TikTok и подтвердите вход.', photo)
                                finally:
                                    value = ''
                    finally:
                        context.close()
                if success and not session.stop.is_set() and self.allowed(session.owner, session.profile):
                    if mode == 'staged':
                        if active.exists():
                            self.adapter.create_auth_backup(session.profile)
                        self.adapter._commit_staged_browser_profile(browser_dir=browser, user_data_dir=active,
                            staged_user_data_dir=target, timestamp=time.strftime('%Y%m%d_%H%M%S'))
                    clear_auth_backoff(root / 'state' / 'auth_backoff.json')
                    (root / 'state' / 'session_import_required.json').unlink(missing_ok=True)
                    record_session_validation(root / 'state', valid=True, source=f'{self.channel}_login')
                    committed = True
                    terminal_stage, terminal_text = 'done', '✅ Вы вошли в TikTok. Можно запускать отправку.'
                elif session.stop.is_set():
                    terminal_stage, terminal_text = 'cancelled', 'Вход отменён.'
                else:
                    terminal_text = 'Время входа вышло. Нажмите «Войти» и попробуйте снова.'
        except Exception as exc:
            # Playwright exceptions can contain a filled password in call logs.
            # Never log or forward the raw exception, even to diagnostics.
            if type(exc) is RuntimeError:
                safe = str(exc) if str(exc) in {
                    'Сначала остановите отправку.', 'Закройте окно входа на компьютере и попробуйте снова.',
                    'Завершите или отмените предыдущий вход на компьютере.',
                    'TikTok просит проверку на картинке. Продолжите вход на компьютере.',
                    'TikTok не принял вход. Проверьте данные и попробуйте снова.',
                    'QR-код не появился. Попробуйте вход по паролю или на компьютере.',
                } else 'Не получилось войти. Попробуйте ещё раз или выберите вход на компьютере.'
            else:
                safe = 'Не получилось открыть вход. Попробуйте ещё раз или выберите вход на компьютере.'
            logger.warning('Interactive TikTok login failed (%s)', type(exc).__name__)
            terminal_text = safe
        finally:
            identifier = value = ''
            with suppress(Exception), browser_lease(browser):
                if (read_json(pending, {}) or {}).get('id') == session.id:
                    if mode == 'active' and not committed:
                        atomic_write_text(root / 'state' / 'session_import_required.json',
                                          json.dumps({'reason': 'interactive_login_not_completed', 'at': time.time()}))
                        record_session_validation(root / 'state', valid=None, source='telegram_login_cancelled')
                    pending.unlink(missing_ok=True)
            if photo:
                with suppress(OSError):
                    photo.unlink(missing_ok=True)
            while not session.inbox.empty():
                with suppress(queue.Empty):
                    session.inbox.get_nowait()
            session.stop.set()
            with self.lock:
                self.sessions.pop(session.id, None)
            with suppress(Exception):
                self._emit(session, terminal_stage, terminal_text)

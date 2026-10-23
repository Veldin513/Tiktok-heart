"""Read a user-copied Chrome request without executing it or saving its text."""
from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from urllib.parse import urlparse

AUTH_COOKIE_NAMES = {'sessionid', 'sessionid_ss', 'sid_tt'}


def cookies_from_curl(text: str) -> list[dict]:
    if not isinstance(text, str):
        raise ValueError('Нужен текст запроса из Chrome.')
    # Chrome offers bash and Windows cmd formats. Only tokenize; never run a shell.
    text = text.strip().replace('^\r\n', '').replace('^\n', '')
    if text.startswith(('curl "', 'curl.exe "')):
        text = text.replace('^"', '"')
    # Copy all as cURL can include megabytes of unrelated requests. Bound only
    # the first command; never tokenize or validate the remaining commands.
    request_limit = 256 * 1024
    truncated = len(text) > request_limit
    ended = False
    try:
        lexer = shlex.shlex(text[:request_limit], posix=True, punctuation_chars=';')
        lexer.whitespace_split = True
        tokens = []
        for token in lexer:
            if token == ';':
                ended = True
                break
            tokens.append(token)
    except ValueError:
        if truncated:
            raise ValueError('Первый запрос слишком большой. Скопируйте только строку messages: Copy as cURL.') from None
        raise ValueError('Не удалось прочитать запрос. Используйте Copy as cURL (bash).') from None
    if truncated and not ended:
        raise ValueError('Первый запрос слишком большой. Скопируйте только строку messages: Copy as cURL.')
    if not tokens or tokens[0] not in ('curl', 'curl.exe'):
        raise ValueError('Нужен запрос из Chrome: Network → Copy → Copy as cURL (bash).')
    urls = [token for token in tokens if token.startswith(('https://', 'http://'))]
    if len(urls) != 1:
        raise ValueError('Скопируйте один запрос страницы https://www.tiktok.com/messages.')
    url = urlparse(urls[0])
    host = (url.hostname or '').lower()
    if url.scheme != 'https' or host != 'www.tiktok.com':
        raise ValueError('Выберите запрос www.tiktok.com, лучше первую строку messages после обновления страницы.')
    values = []
    for index, token in enumerate(tokens[:-1]):
        value = tokens[index + 1]
        if token in ('-b', '--cookie'):
            values.append(value)
        elif token in ('-H', '--header') and value.lower().startswith('cookie:'):
            values.append(value.split(':', 1)[1].strip())
    cookies = {}
    for value in values:
        for pair in value.split(';'):
            name, sep, content = pair.strip().partition('=')
            if sep and re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', name) and content:
                if any(ord(char) < 32 or ord(char) == 127 for char in content):
                    raise ValueError('В cookies найдены недопустимые символы. Скопируйте запрос заново.')
                cookies[name] = {
                    'name': name, 'value': content, 'domain': '.tiktok.com', 'path': '/',
                    'secure': True, 'httpOnly': name in AUTH_COOKIE_NAMES,
                    'sameSite': 'Lax', 'expires': time.time() + 30 * 24 * 3600,
                }
    if not AUTH_COOKIE_NAMES.intersection(cookies):
        raise ValueError('В запросе нет cookies входа. Откройте сообщения в авторизованном Chrome и скопируйте запрос messages.')
    return list(cookies.values())


def prepare_session(user_data_dir: Path, cookies: list[dict]) -> None:
    """Validate in an isolated profile using the project's existing Chromium."""
    from playwright.sync_api import sync_playwright

    from yara_app.auth_flow import _launch_persistent_chromium

    try:
        with sync_playwright() as playwright:
            context = _launch_persistent_chromium(playwright, str(user_data_dir), headless=True)
            try:
                context.add_cookies(cookies)
                page = context.pages[0] if context.pages else context.new_page()
                response = page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=45000)
                if response is None or response.status >= 400:
                    raise RuntimeError('session unavailable')
                # A /messages URL alone is insufficient: a logged-out page may keep it.
                page.locator('#main-content-messages:visible, [data-e2e="inbox-icon"]:visible, .css-19p0p2f-DivInboxContainer:visible').first.wait_for(
                    state='visible', timeout=20000,
                )
                if page.locator('[data-e2e="top-login-button"]:visible').count():
                    raise RuntimeError('session unavailable')
                host = (urlparse(page.url).hostname or '').lower()
                if host not in ('www.tiktok.com', 'tiktok.com') or '/login' in urlparse(page.url).path:
                    raise RuntimeError('session unavailable')
            finally:
                context.close()
    except Exception:
        # Playwright exceptions can contain cookie values. Never return raw errors.
        raise RuntimeError(
            'Вход не подтверждён. Текущий профиль сохранён. Проверьте, что Chrome открывает сообщения TikTok, '
            'и скопируйте свежий запрос messages. Возможны также ограничение TikTok или проблема подключения.'
        ) from None

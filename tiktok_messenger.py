from __future__ import annotations

"""Messaging helpers for the TikTok inbox.

This module deliberately keeps the historically working selectors and click
mechanics intact. The refactor focuses on structure, compatibility, and clearer
fall-back handling rather than altering browser behaviour.
"""

import logging
import re
import unicodedata
from typing import Iterable

from ttbot.models import ChatOpenMethod, ChatOpenResult

logger = logging.getLogger(__name__)
MESSAGES_URL = 'https://www.tiktok.com/messages'
INBOX_ITEM_SELECTOR = '[data-e2e="inbox-list-item"]'
EDITOR_SELECTOR = '.public-DraftEditor-content'
MESSAGE_BUTTON_PATTERN = re.compile(r'Сообщение|Message', re.IGNORECASE)


def _soft_wait(page, timeout_ms: int) -> None:
    waiter = getattr(page, 'wait_for_timeout', None)
    if callable(waiter):
        try:
            waiter(timeout_ms)
        except Exception:
            pass


def _dom_click(locator) -> None:
    """Click via Playwright first, then via DOM as a safe fallback."""

    try:
        locator.click(timeout=1200)
        return
    except Exception:
        pass
    locator.evaluate('node => node.click()')


def _wait_for_editor(page, timeout_ms: int = 2500) -> bool:
    try:
        page.locator(EDITOR_SELECTOR).wait_for(state='attached', timeout=timeout_ms)
        return True
    except Exception:
        return False


def _normalize_target_name(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value or '').casefold()
    return ' '.join(normalized.split())


def _names_match(expected: str, candidate: str) -> bool:
    expected_norm = _normalize_target_name(expected)
    candidate_norm = _normalize_target_name(candidate)
    return bool(expected_norm and candidate_norm and (expected_norm in candidate_norm or candidate_norm in expected_norm))


def _iter_inbox_items(page, *, visible_first: bool = True) -> Iterable[object]:
    """Yield inbox items with a stable visibility-first ordering.

    We intentionally prefer visible rows so Plan A keeps behaving like before.
    Hidden rows are only attempted as a last resort for quirky virtualized lists
    and uncommon display-name rendering cases.
    """

    items = page.locator(INBOX_ITEM_SELECTOR)
    try:
        total = items.count()
    except Exception:
        total = 0

    visible_items: list[object] = []
    hidden_items: list[object] = []
    for index in range(total):
        locator = items.nth(index)
        try:
            if locator.is_visible():
                visible_items.append(locator)
            else:
                hidden_items.append(locator)
        except Exception:
            hidden_items.append(locator)

    ordered_items = visible_items + hidden_items if visible_first else hidden_items + visible_items
    for locator in ordered_items:
        yield locator


def _open_chat_by_strict_list(page, target_name: str) -> bool:
    """Fallback: scan all inbox rows with unicode-normalised matching.

    Also tries scrolling the inbox list to reveal virtualised rows that
    are not yet in the DOM (TikTok uses a windowed list).
    """
    import unicodedata as _ud

    try:
        page.locator(INBOX_ITEM_SELECTOR).first.wait_for(state='attached', timeout=5000)
    except Exception:
        return False

    _nfkc = _ud.normalize('NFKC', target_name)
    _slug = _ud.normalize('NFKD', _nfkc).encode('ascii', 'ignore').decode('ascii').lower()

    def _any_match(candidate_text: str) -> bool:
        if _names_match(target_name, candidate_text):
            return True
        if _slug and len(_slug) >= 3:
            slug2 = _ud.normalize('NFKD', _ud.normalize('NFKC', candidate_text)
                    ).encode('ascii', 'ignore').decode('ascii').lower()
            if _slug in slug2:
                return True
        return False

    def _scan_current_rows() -> bool:
        for locator in _iter_inbox_items(page, visible_first=True):
            try:
                candidate_text = locator.inner_text()
            except Exception:
                continue
            if _item_is_activity(candidate_text):
                continue
            if not _any_match(candidate_text):
                continue
            _dom_click(locator)
            if _wait_for_editor(page, 2500):
                return True
        return False

    # First pass on current rows
    if _scan_current_rows():
        logger.info('✅ Чат найден в списке (строгий скан, 1-й проход)!')
        return True

    # Try scrolling inbox to load more virtualised rows (up to 3 scroll attempts)
    try:
        inbox_list = page.locator('[class*="DivInboxList"], [class*="inbox-list"], '
                                  '[data-e2e="inbox-list"], div[class*="Inbox"]').first
        for _ in range(3):
            try:
                inbox_list.evaluate('el => el.scrollBy(0, 400)')
                _soft_wait(page, 400)
                if _scan_current_rows():
                    logger.info('✅ Чат найден в списке после прокрутки!')
                    return True
            except Exception:
                break
    except Exception:
        pass

    return False


def _open_chat_from_inbox_items(page, target_name: str, *, visible: bool) -> bool:
    """Try inbox rows with the requested visibility without changing click logic."""

    for locator in _iter_inbox_items(page, visible_first=visible):
        try:
            if locator.is_visible() != visible:
                continue
            candidate_text = locator.inner_text()
        except Exception:
            continue
        # Skip activity/notification items — they are not chat conversations
        if _item_is_activity(candidate_text):
            continue
        if target_name not in candidate_text and not _names_match(target_name, candidate_text):
            continue
        _dom_click(locator)
        if _wait_for_editor(page, 2500):
            return True
    return False


# Patterns that indicate an ACTIVITY/NOTIFICATION item (not a DM conversation)
_ACTIVITY_PATTERNS = ('подписал', 'упомянул', 'лайк', 'понравил',
                      'follow', 'mention', 'liked', 'reacted')


def _item_is_activity(text: str) -> bool:
    """Return True if an inbox item looks like a notification, not a DM chat."""
    low = text.lower()
    return any(p in low for p in _ACTIVITY_PATTERNS)


def _ensure_messages_tab(page) -> None:
    """Make sure TikTok is showing the DM (Messages) tab, not Activity.

    TikTok /messages has two sub-tabs: Activity and Messages (DMs).
    After navigation it sometimes defaults to Activity.

    Strategy (each step is tried in order, stops on first success):
    1. data-e2e="message-tab" / "inbox-message-tab"  — stable selector
    2. Any <a href*="/messages"> that is currently NOT selected/active
       (TikTok renders these as sibling tab links inside the inbox page)
    3. JavaScript: find all tab-like elements and click the "Messages" one
    """

    # Step 1 — preferred, stable
    for sel in ('[data-e2e="message-tab"]', '[data-e2e="inbox-message-tab"]'):
        try:
            el = page.locator(sel).first
            el.wait_for(state='attached', timeout=1500)
            if el.is_visible():
                _dom_click(el)
                _soft_wait(page, 600)
                logger.info('Messages tab clicked via %s', sel)
                return
        except Exception:
            continue

    # Step 2 — find the non-active /messages link (the sibling sub-tab)
    # TikTok renders: <a href="/messages?...">Activity</a> | <a href="/messages">Messages</a>
    # The currently-active tab often gets an aria-selected="true" or a highlight class
    try:
        js_result = page.evaluate("""
            () => {
                // Find all anchor/button elements that look like inbox sub-tabs
                const candidates = [
                    ...document.querySelectorAll('a[href*="/messages"]'),
                    ...document.querySelectorAll('[data-e2e*="message"]'),
                    ...document.querySelectorAll('[data-e2e*="inbox"]'),
                ];
                for (const el of candidates) {
                    const text = (el.innerText || el.textContent || '').toLowerCase();
                    const isActive = el.getAttribute('aria-selected') === 'true'
                        || el.classList.toString().includes('active')
                        || el.classList.toString().includes('selected');
                    // We want the "Messages / Сообщения" tab that is NOT currently active
                    if (!isActive && (text.includes('message') || text.includes('сообщени'))) {
                        el.click();
                        return 'clicked:' + text.trim().slice(0, 30);
                    }
                }
                return 'not_found';
            }
        """)
        if js_result and js_result.startswith('clicked:'):
            _soft_wait(page, 700)
            logger.info('Messages tab clicked via JS: %s', js_result)
            return
    except Exception:
        pass

    # Step 3 — last resort: click any element with text "Messages"/"Сообщения"
    for text_frag in ('Messages', 'Сообщения', 'Сообщени'):
        try:
            el = page.get_by_text(text_frag, exact=False).first
            el.wait_for(state='visible', timeout=1000)
            _dom_click(el)
            _soft_wait(page, 600)
            logger.info('Messages tab clicked via text "%s"', text_frag)
            return
        except Exception:
            continue


def open_messages(page) -> None:
    """Navigate to the TikTok DM inbox (Messages sub-tab).

    TikTok /messages can default to the Activity (notifications) view.
    We navigate there, wait for the page to fully settle, then switch
    to the DM sub-tab if needed.
    """
    page.goto(MESSAGES_URL, wait_until='domcontentloaded')
    _soft_wait(page, 700)  # let the page fully render before probing tabs

    # Wait for inbox items to appear first (tells us the page is interactive)
    try:
        page.locator(INBOX_ITEM_SELECTOR).first.wait_for(state='attached', timeout=6000)
    except Exception:
        _soft_wait(page, 400)
        return

    # Check if we landed on Activity; if so, switch to Messages
    try:
        first_text = page.locator(INBOX_ITEM_SELECTOR).first.inner_text()
        if _item_is_activity(first_text):
            logger.info('Activity tab detected — switching to Messages.')
            _ensure_messages_tab(page)
            # Give the DM list time to load after tab switch
            _soft_wait(page, 500)
            page.locator(INBOX_ITEM_SELECTOR).first.wait_for(
                state='attached', timeout=5000)
    except Exception:
        pass


def _try_open_locator(locator, page, *, wait_timeout_ms: int, editor_timeout_ms: int) -> bool:
    locator.wait_for(state='attached', timeout=wait_timeout_ms)
    _dom_click(locator)
    return _wait_for_editor(page, editor_timeout_ms)


def open_chat_by_list(page, target_name: str) -> bool:
    """Try the original Plan A first, then repair it with DOM-row matching.

    The first project version used two exact-text lookups:
    1. visible text node;
    2. broader exact lookup without a visibility filter.

    That exact sequence stays first so the historical behaviour is preserved.
    If both exact lookups miss, we do a conservative recovery pass over the
    already-rendered inbox rows: visible rows first, hidden rows second, using
    Unicode-normalized text comparison. Click mechanics stay unchanged.
    """

    import unicodedata as _ud
    name_nfkc = _ud.normalize('NFKC', target_name)

    page.locator(INBOX_ITEM_SELECTOR).first.wait_for(state='attached', timeout=15000)
    _soft_wait(page, 350)
    logger.info('Ищу %s в списке слева...', target_name)

    # Step 1: exact visible-text search — try original name then NFKC variant
    for _name in dict.fromkeys([target_name, name_nfkc]):
        try:
            loc = page.get_by_text(_name).filter(visible=True).first
            if _try_open_locator(loc, page, wait_timeout_ms=1500, editor_timeout_ms=3000):
                logger.info('✅ Чат найден в списке (visible exact, вариант «%s»)!', _name)
                return True
        except Exception:
            pass

    # Step 2: broad search (includes hidden locators) — try original name then NFKC
    for _name in dict.fromkeys([target_name, name_nfkc]):
        try:
            loc = page.get_by_text(_name).first
            if _try_open_locator(loc, page, wait_timeout_ms=1500, editor_timeout_ms=2500):
                logger.info('✅ Чат найден в списке (broad, вариант «%s»)!', _name)
                return True
        except Exception:
            pass

    # Step 3+4: DOM row scan — unicode-normalised matching, visible then hidden
    if _open_chat_from_inbox_items(page, target_name, visible=True):
        logger.info('✅ Чат найден в списке (DOM visible)!')
        return True
    if _open_chat_from_inbox_items(page, target_name, visible=False):
        logger.info('✅ Чат найден в списке (DOM hidden)!')
        return True

    # Step 5: strict normalised fallback (with ASCII slug matching)
    if _open_chat_by_strict_list(page, target_name):
        return True

    logger.info('План А не сработал. Использую план Б: прямой переход...')
    return False


def open_chat_by_profile(page, profile_url: str) -> bool:
    final_url = profile_url if profile_url.startswith('http') else f'https://www.tiktok.com/{profile_url}'
    page.goto(final_url, wait_until='domcontentloaded')
    logger.info("Ищу кнопку 'Сообщение' в профиле...")
    msg_button = page.locator('button').filter(has_text=MESSAGE_BUTTON_PATTERN).first
    msg_button.wait_for(state='attached', timeout=15000)
    _dom_click(msg_button)
    page.wait_for_url('**/messages**', timeout=15000)
    logger.info('✅ Успешно вошли в чат через профиль!')
    return True


def send_message(page, text: str) -> None:
    logger.info('Ожидание поля ввода...')
    input_box = page.locator(EDITOR_SELECTOR)
    input_box.wait_for(state='attached', timeout=15000)
    input_box.evaluate('node => node.focus()')
    logger.info('Печатаем сообщение: %s', text)
    page.keyboard.insert_text(text)
    page.wait_for_timeout(250)
    page.keyboard.press('Enter')


def send_heart(page, symbol: str) -> None:
    """Backward-compatible alias kept for the refactored service layer."""

    send_message(page, symbol)


class ChatOpener:
    """Open a TikTok chat using the first project's original strategy order.

    The first version of the project did only two things here:
    1. try Plan A in the inbox list;
    2. if that fails, open the profile and press the message button.

    The stricter unicode-normalized list scan is kept in this module as an
    internal helper for future diagnostics, but it is *not* part of the default
    runtime path because the user requested the original behaviour to be
    restored.
    """

    def open_chat(self, page, target_name: str, profile_url: str | None = None) -> ChatOpenResult:
        try:
            if open_chat_by_list(page, target_name):
                return ChatOpenResult(ok=True, method=ChatOpenMethod.LEGACY_LIST)
        except Exception as exc:
            logger.info('Legacy list strategy failed for %s: %s', target_name, exc)

        if profile_url:
            try:
                if open_chat_by_profile(page, profile_url):
                    return ChatOpenResult(ok=True, method=ChatOpenMethod.PROFILE)
            except Exception as exc:
                logger.info('Profile strategy failed for %s: %s', target_name, exc)
                return ChatOpenResult(ok=False, reason='profile_open_failed')

        return ChatOpenResult(ok=False, reason='chat_open_failed')


__all__ = [
    'ChatOpener',
    'MESSAGES_URL',
    'open_messages',
    'open_chat_by_list',
    'open_chat_by_profile',
    'send_message',
    'send_heart',
]

"""TikTok DM navigation, recipient verification, and confirmed sending."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Iterable
from contextlib import suppress
from urllib.parse import unquote, urlparse

from yara_app.auth_flow import explicitly_logged_out
from yara_app.delivery import (
    DeliveryRejected,
    DeliveryUnconfirmed,
    SendNotAttempted,
    SendReceipt,
    attach_delivery_tracker,
)

logger = logging.getLogger(__name__)
MESSAGES_URL = 'https://www.tiktok.com/messages'
INBOX_ITEM_SELECTOR = '[data-e2e="inbox-list-item"]'
DM_ITEM_SELECTOR = '[data-e2e="dm-new-conversation-item"]'
DM_NAME_SELECTOR = '[data-e2e="dm-new-conversation-nickname"]'
EDITOR_SELECTOR = '.public-DraftEditor-content'
MESSAGE_BUTTON_PATTERN = re.compile(r'Сообщение|Message', re.IGNORECASE)
MESSAGE_BUTTON_TEXTS = (
    'Message',
    'Messages',
    'Send message',
    'Сообщение',
    'Сообщения',
    'Написать',
    'Написать сообщение',
)


def _soft_wait(page, timeout_ms: int) -> None:
    waiter = getattr(page, 'wait_for_timeout', None)
    if callable(waiter):
        with suppress(Exception):
            waiter(timeout_ms)


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
        page.locator(EDITOR_SELECTOR).wait_for(state='visible', timeout=timeout_ms)
        return True
    except Exception:
        return False


def _normalize_target_name(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value or '').casefold()
    return ' '.join(normalized.split())


def _names_match(expected: str, candidate: str) -> bool:
    expected_norm = _normalize_target_name(expected)
    first_line = next((line for line in (candidate or '').splitlines() if line.strip()), '')
    return bool(expected_norm and expected_norm == _normalize_target_name(first_line))


def _profile_handle(value: str) -> str:
    value = str(value or '').strip()
    if not value.startswith('@'):
        parsed = urlparse(value)
        if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in {'www.tiktok.com', 'tiktok.com'}:
            return ''
        value = unquote(parsed.path).rstrip('/')
        if not value.startswith('/@'):
            return ''
        value = value[1:]
    handle = value[1:]
    return handle.casefold() if handle and not any(c.isspace() or c in '/?#' for c in handle) else ''


def dismiss_chat_notice(page) -> None:
    """Dismiss only an explicit close/later control; never click consent/OK."""
    overlays = page.locator('.TUXModal-overlay:visible, [role="dialog"][aria-modal="true"]:visible')
    for _ in range(2):
        if not overlays.count():
            return
        overlay = overlays.first
        scopes = [overlay, overlay.locator('..')]
        closed = False
        for scope in scopes:
            close = scope.get_by_role(
                'button', name=re.compile(r'^(Close|Закрыть|Not now|Не сейчас|Later|Позже)$', re.IGNORECASE),
            ).filter(visible=True)
            if close.count() != 1:
                continue
            try:
                close.click(timeout=1500)
                overlay.wait_for(state='hidden', timeout=2000)
                closed = True
                logger.info('Информационное окно TikTok закрыто кнопкой закрытия.')
                break
            except Exception:
                continue
        if not closed:
            raise SendNotAttempted(
                'Всплывающее окно TikTok перекрывает чат. Закройте его вручную через отдельный Chrome '
                'в разделе «Браузер», затем проверьте вход. Отправка не начиналась.'
            )
    if overlays.count():
        raise SendNotAttempted('Всплывающее окно TikTok осталось открытым. Отправка не начиналась.')


def _chat_profile_verified(page, profile_url: str | None) -> bool:
    if not profile_url:
        return True
    expected = _profile_handle(profile_url)
    if not expected:
        return False
    identifier = page.locator('[data-e2e="chat-uniqueid"]:visible')
    try:
        identifier.filter(has_text=re.compile(r'^\s*@?' + re.escape(expected) + r'\s*$', re.IGNORECASE)).first.wait_for(
            state='visible', timeout=3000,
        )
        actual = identifier.first.inner_text().strip()
        if not actual.startswith('@'):
            actual = '@' + actual
        return identifier.count() == 1 and _profile_handle(actual) == expected
    except Exception:
        return False


def _open_modern_chat(page, target_name: str, profile_url: str | None) -> bool | None:
    rows = page.locator(DM_ITEM_SELECTOR)
    # Some older layouts do not have these rows. Keep the legacy path for them.
    if not callable(getattr(rows, 'locator', None)):
        return None
    try:
        rows.first.wait_for(state='visible', timeout=4500)
    except Exception:
        return None
    dismiss_chat_notice(page)
    deadline = time.monotonic() + 3.0
    while True:
        matches = []
        for index in range(rows.count()):
            with suppress(Exception):
                row = rows.nth(index)
                title = row.locator(DM_NAME_SELECTOR).first
                if row.is_visible() and title.count() and _names_match(target_name, title.inner_text()):
                    matches.append(row)
        if matches or time.monotonic() >= deadline:
            break
        # The first row can be interactive while the target's nickname is still
        # loading. Re-read locators; do not freeze the first incomplete snapshot.
        _soft_wait(page, 150)
    if len(matches) != 1:
        logger.info('В списке сообщений нет единственного совпадения имени адресата.')
        return False
    # Click the conversation row, not a link to the public profile.
    try:
        matches[0].click(timeout=3000)
    except Exception:
        dismiss_chat_notice(page)
        return False
    if not _wait_for_editor(page, 5000):
        return False
    dismiss_chat_notice(page)
    if not _chat_profile_verified(page, profile_url):
        logger.warning('Открытый чат не совпал с @username адресата. Отправка через список отменена.')
        return False
    logger.info('✅ Чат открыт через список сообщений; адресат проверен.')
    return True


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
    try:
        page.locator(INBOX_ITEM_SELECTOR).first.wait_for(state='attached', timeout=5000)
    except Exception:
        return False

    def _any_match(candidate_text: str) -> bool:
        return _names_match(target_name, candidate_text)

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
        if not _names_match(target_name, candidate_text):
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

    # Modern DMs coexist with hidden legacy notification rows. Do not mistake
    # those hidden rows for an active Activity tab or click the navigation again.
    try:
        page.locator(DM_ITEM_SELECTOR).first.wait_for(state='visible', timeout=4500)
        return
    except Exception:
        pass

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


def _try_open_locator(
    locator, page, *, wait_timeout_ms: int, editor_timeout_ms: int,
    profile_url: str | None = None, unique: bool = False,
) -> bool:
    first = locator.first
    first.wait_for(state='attached', timeout=wait_timeout_ms)
    if unique and locator.count() != 1:
        return False
    dismiss_chat_notice(page)
    _dom_click(first)
    if not _wait_for_editor(page, editor_timeout_ms):
        return False
    dismiss_chat_notice(page)
    return _chat_profile_verified(page, profile_url)


def _wait_for_chat_after_profile_click(page) -> bool:
    _soft_wait(page, 700)
    if _wait_for_editor(page, 3000):
        return True
    with suppress(Exception):
        page.wait_for_url('**/messages**', timeout=8000, wait_until='domcontentloaded')
    return _wait_for_editor(page, 7000)


def _inbox_items_attached(page, timeout_ms: int = 1200) -> bool:
    try:
        page.locator(INBOX_ITEM_SELECTOR).first.wait_for(state='attached', timeout=timeout_ms)
        return True
    except Exception:
        return False


def open_chat_by_list(page, target_name: str, profile_url: str | None = None) -> bool:
    """Keep historical name lookup timing first, then scan DM rows if needed."""

    import unicodedata as _ud
    name_nfkc = _ud.normalize('NFKC', target_name)

    _soft_wait(page, 350)
    logger.info('Ищу %s в списке слева...', target_name)

    # Preserve the old text-node click, including decorated nicknames. In the
    # new layout restrict it to nickname nodes so message previews/notifications
    # cannot become candidates. A configured URL is verified after every click.
    modern_layout = callable(getattr(page.get_by_text(target_name), 'and_', None)) and bool(page.locator(DM_ITEM_SELECTOR).count())

    def _name_locator(name):
        loc = page.get_by_text(name, exact=not bool(profile_url))
        if modern_layout:
            loc = loc.and_(page.locator(f'{DM_NAME_SELECTOR}, {DM_NAME_SELECTOR} *'))
        return loc

    # Step 1: exact visible-text search — try original name then NFKC variant
    for _name in dict.fromkeys([target_name, name_nfkc]):
        try:
            loc = _name_locator(_name).filter(visible=True)
            if _try_open_locator(loc, page, wait_timeout_ms=1500, editor_timeout_ms=3000,
                                 profile_url=profile_url, unique=modern_layout):
                logger.info('✅ Чат найден в списке (visible exact, вариант «%s»)!', _name)
                return True
        except SendNotAttempted:
            raise
        except Exception:
            pass

    # Step 2: broad search (includes hidden locators) — try original name then NFKC
    for _name in dict.fromkeys([target_name, name_nfkc]):
        try:
            loc = _name_locator(_name)
            if _try_open_locator(loc, page, wait_timeout_ms=1500, editor_timeout_ms=2500,
                                 profile_url=profile_url, unique=modern_layout):
                logger.info('✅ Чат найден в списке (broad, вариант «%s»)!', _name)
                return True
        except SendNotAttempted:
            raise
        except Exception:
            pass

    modern = _open_modern_chat(page, target_name, profile_url)
    if modern is not None:
        return modern
    if profile_url:
        # No verified DM/header: do not recover by clicking hidden notifications.
        return False

    # Step 3+4: DOM row scan — unicode-normalised matching, visible then hidden.
    # Do not make this selector mandatory: TikTok sometimes renders the inbox
    # without the old data-e2e attribute, while the visible-name lookup above
    # still works.
    if _inbox_items_attached(page):
        if _open_chat_from_inbox_items(page, target_name, visible=True):
            logger.info('✅ Чат найден в списке (DOM visible)!')
            return True
        if _open_chat_from_inbox_items(page, target_name, visible=False):
            logger.info('✅ Чат найден в списке (DOM hidden)!')
            return True

        # Step 5: strict normalised fallback (with ASCII slug matching)
        if _open_chat_by_strict_list(page, target_name):
            return True
    else:
        logger.info('Inbox data-e2e rows are not attached; profile fallback is allowed only after name lookup failed.')

    logger.info('План А не сработал. Использую план Б: прямой переход...')
    return False


def open_chat_by_profile(page, profile_url: str) -> bool:
    final_url = profile_url if profile_url.startswith('http') else f'https://www.tiktok.com/{profile_url}'
    page.goto(final_url, wait_until='domcontentloaded')
    logger.info("Ищу кнопку 'Сообщение' в профиле...")
    opened = False
    for selector in ('[data-e2e="message-button"]', 'button[data-e2e*="message"]'):
        try:
            candidate = page.locator(selector).first
            candidate.wait_for(state='attached', timeout=2500)
            _dom_click(candidate)
            opened = True
            break
        except Exception:
            continue
    if not opened:
        for label in MESSAGE_BUTTON_TEXTS:
            try:
                candidate = page.get_by_role('button', name=label, exact=True).first
                candidate.wait_for(state='attached', timeout=1500)
                _dom_click(candidate)
                opened = True
                break
            except Exception:
                continue
    if not opened:
        msg_button = page.locator('button').filter(has_text=MESSAGE_BUTTON_PATTERN).first
        msg_button.wait_for(state='attached', timeout=5000)
        _dom_click(msg_button)
    if not _wait_for_chat_after_profile_click(page):
        raise RuntimeError('Profile message button was clicked, but chat editor did not become ready.')
    dismiss_chat_notice(page)
    logger.info('✅ Успешно вошли в чат через профиль!')
    return True


def send_message(page, text: str, *, confirmation_timeout_ms: int = 15000) -> SendReceipt:
    tracker = attach_delivery_tracker(page)
    logger.info('Ожидание поля ввода...')
    input_box = page.locator(EDITOR_SELECTOR)
    input_box.wait_for(state='visible', timeout=15000)
    dismiss_chat_notice(page)
    if input_box.inner_text(timeout=2000).strip():
        raise SendNotAttempted('В поле ввода есть черновик. Отправка не выполнялась.')
    try:
        input_box.click(timeout=5000)
    except Exception:
        raise SendNotAttempted('Поле ввода TikTok недоступно или перекрыто. Отправка не начиналась.') from None
    page.keyboard.insert_text(text)
    if input_box.inner_text(timeout=2000).strip() != text.strip():
        raise SendNotAttempted('Текст не появился в поле ввода. Отправка не выполнялась.')
    tracker.begin(text)
    try:
        send_button = page.locator('[data-e2e="message-send"]:visible').first
        if send_button.count():
            if not send_button.is_enabled():
                raise SendNotAttempted('Кнопка отправки недоступна. Отправка не выполнялась.')
            send_button.click(timeout=3000)
        else:
            page.keyboard.press('Enter')
        deadline = time.monotonic() + confirmation_timeout_ms / 1000
        while time.monotonic() < deadline:
            if tracker.rejection:
                raise DeliveryRejected(tracker.rejection)
            if tracker.receipt is not None:
                return tracker.receipt
            if explicitly_logged_out(page):
                raise DeliveryUnconfirmed('Вход потерян во время отправки. Приём сообщения не подтверждён.')
            page.wait_for_timeout(200)
        raise DeliveryUnconfirmed('TikTok не подтвердил приём сообщения. Результат неизвестен; успех не записан.')
    except (DeliveryUnconfirmed, DeliveryRejected, SendNotAttempted):
        raise
    except Exception:
        raise DeliveryUnconfirmed('Не удалось проверить результат отправки; успех не записан.') from None
    finally:
        if tracker.receipt is None:
            # Remove only the exact text this attempt put into an initially
            # empty composer. Do not erase a different/user-authored draft.
            with suppress(Exception):
                if input_box.inner_text(timeout=1000).strip() == text.strip():
                    input_box.fill('')
        tracker.end()


__all__ = [
    'MESSAGES_URL',
    'open_messages',
    'open_chat_by_list',
    'open_chat_by_profile',
    'send_message',
]

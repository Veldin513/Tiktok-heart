from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
auth_logger = logging.getLogger("auth_debug")

_BENIGN_CLOSE_ERRORS = ('event loop is closed', 'target page, context or browser has been closed', 'browser has been closed', 'target closed')


def init_work_browser(playwright, user_data_dir):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel='chrome',
        headless=True,
        args=['--disable-blink-features=AutomationControlled', '--mute-audio', '--disable-infobars', '--disable-dev-shm-usage', '--no-sandbox'],
        viewport={'width': 1280, 'height': 720},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    )


def init_auth_browser(playwright, user_data_dir):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel='chrome',
        headless=False,
        viewport={'width': 1280, 'height': 720},
    )


def block_heavy_content(route, block_media: bool):
    request_url = route.request.url
    resource_type = route.request.resource_type
    try:
        if block_media and resource_type in ['image', 'media', 'font']:
            route.abort()
        elif block_media and any(x in request_url for x in ['/log/', '/analytics/', 'sentry', 'mon.tiktok.com']):
            route.abort()
        else:
            route.continue_()
    except BaseException:
        pass


def get_latest_tiktok_page(context, fallback_page=None):
    try:
        candidates = []
        for p in context.pages:
            try:
                url = (p.url or '').lower()
            except Exception:
                url = ''
            if 'tiktok.com' in url or url in ('', 'about:blank'):
                candidates.append(p)
        if candidates:
            return candidates[-1]
    except Exception as exc:
        logger.warning('Не удалось выбрать актуальную TikTok-вкладку: %s', exc)
    return fallback_page


def dump_auth_state(page, stage):
    try:
        cookies = page.context.cookies()
        tt_cookies = [c for c in cookies if 'tiktok' in c.get('domain', '').lower()]
        state = page.context.storage_state()
        tt_origins = [o.get('origin', '') for o in state.get('origins', []) if 'tiktok' in o.get('origin', '').lower()]
        auth_logger.info('[AUTH][%s] url=%s | pages=%s | tt_cookies=%s | tt_origins=%s', stage, page.url, len(page.context.pages), len(tt_cookies), tt_origins)
    except Exception as exc:
        auth_logger.warning('[AUTH][%s] Не удалось снять storage state: %s', stage, exc)


_AUTH_DEBUG_PAGES = set()
_AUTH_DEBUG_CONTEXTS = set()


def attach_page_auth_debug(page, auth_runtime):
    page_key = id(page)
    if page_key in _AUTH_DEBUG_PAGES:
        return
    _AUTH_DEBUG_PAGES.add(page_key)

    def _is_interesting(url):
        url = (url or '').lower()
        markers = ['tiktok.com', 'passport', 'login', 'logout', 'auth', 'qr', 'session', 'messages']
        return any(marker in url for marker in markers)

    def _dump_qr_response(response):
        url = (response.url or '').lower()
        if 'check_qrconnect' not in url:
            return
        try:
            payload = response.json()
            auth_logger.info('[AUTH][QR_JSON] %s %s', response.status, json.dumps(payload, ensure_ascii=False)[:1500])
            data = payload.get('data') or {}
            message = payload.get('message')
            error_code = data.get('error_code')
            description = data.get('description', '')
            if message == 'error' and error_code is not None:
                auth_runtime['qr_error_code'] = error_code
                auth_runtime['qr_error_text'] = description
                auth_runtime['qr_error_count'] = auth_runtime.get('qr_error_count', 0) + 1
                now_ts = time.time()
                if auth_runtime.get('qr_error_first_seen_ts') is None:
                    auth_runtime['qr_error_first_seen_ts'] = now_ts
                auth_runtime['qr_error_last_seen_ts'] = now_ts
                auth_logger.warning('[AUTH][QR_ERROR] code=%s description=%s count=%s', error_code, description, auth_runtime['qr_error_count'])
        except Exception:
            try:
                text = response.text()
                auth_logger.info('[AUTH][QR_TEXT] %s %s', response.status, text[:1500].replace(chr(10), ' '))
            except Exception as exc:
                auth_logger.warning('[AUTH][QR_BODY_READ_FAILED] %s', exc)

    def _handle_response(response):
        if _is_interesting(response.url):
            auth_logger.info('[AUTH][RESPONSE] %s %s', response.status, response.url)
        _dump_qr_response(response)

    page.on('response', _handle_response)
    page.on('requestfailed', lambda request: auth_logger.warning('[AUTH][REQUESTFAILED] %s | %s', request.failure, request.url) if _is_interesting(request.url) else None)
    page.on('websocket', lambda ws: auth_logger.info('[AUTH][WEBSOCKET] %s', ws.url))
    page.on('framenavigated', lambda frame: auth_logger.info('[AUTH][NAVIGATED] %s', frame.url) if frame == page.main_frame else None)


def attach_auth_debug_listeners(context, auth_runtime):
    context_key = id(context)
    if context_key in _AUTH_DEBUG_CONTEXTS:
        return
    _AUTH_DEBUG_CONTEXTS.add(context_key)
    for p in context.pages:
        attach_page_auth_debug(p, auth_runtime)
    context.on('page', lambda new_page: (auth_logger.info('[AUTH][NEW_PAGE] opened: %s', new_page.url), attach_page_auth_debug(new_page, auth_runtime)))
    context.on('weberror', lambda web_error: auth_logger.error('[AUTH][WEBERROR] %s', web_error.error))


def safe_unroute_all(obj: Any) -> None:
    if obj is None:
        return
    try:
        if hasattr(obj, 'unroute_all'):
            try:
                obj.unroute_all(behavior='ignoreErrors')
            except TypeError:
                obj.unroute_all()
            return
        obj.unroute('**/*')
    except Exception:
        pass


def safe_close_context(context, page=None) -> None:
    if context is None:
        return
    safe_unroute_all(page)
    safe_unroute_all(context)
    time.sleep(0.15)
    try:
        context.close()
    except Exception as exc:
        text = str(exc).lower()
        if any(marker in text for marker in _BENIGN_CLOSE_ERRORS):
            logger.warning('Контекст браузера уже закрыт или завершился с ошибкой: %s', exc)
        else:
            logger.exception('Не удалось корректно закрыть браузерный контекст: %s', exc)


def reset_qr_runtime(auth_runtime: dict) -> None:
    auth_runtime["qr_error_code"] = None
    auth_runtime["qr_error_text"] = ""
    auth_runtime["qr_error_notified"] = False
    auth_runtime["qr_error_count"] = 0
    auth_runtime["qr_error_first_seen_ts"] = None
    auth_runtime["qr_error_last_seen_ts"] = None
    auth_runtime["qr_opened_ts"] = None


def is_logged_in(page) -> bool:
    if "tiktok.com/messages" in page.url:
        return True

    logged_in_elements = [
        '[data-e2e="inbox-icon"]',
        '[data-e2e="profile-icon"]',
        '.css-19p0p2f-DivInboxContainer',
    ]
    for selector in logged_in_elements:
        try:
            if page.locator(selector).is_visible():
                return True
        except Exception:
            continue
    return False


def get_tiktok_auth_fingerprint(page) -> dict:
    try:
        state = page.context.storage_state()
        cookies = sorted(
            (
                c.get("name", ""),
                c.get("domain", ""),
                c.get("path", ""),
                bool(c.get("value")),
            )
            for c in state.get("cookies", [])
            if "tiktok" in c.get("domain", "").lower()
        )

        origins = []
        for origin in state.get("origins", []):
            origin_name = origin.get("origin", "").lower()
            if "tiktok" not in origin_name:
                continue

            local_storage_items = sorted(
                (item.get("name", ""), bool(item.get("value")))
                for item in origin.get("localStorage", [])
            )
            origins.append((origin_name, tuple(local_storage_items)))

        origins.sort()
        return {"cookies": tuple(cookies), "origins": tuple(origins)}
    except Exception as exc:
        auth_logger.warning("Не удалось снять auth fingerprint: %s", exc)
        return {"cookies": tuple(), "origins": tuple()}


def auth_fingerprint_changed(before: dict, after: dict) -> bool:
    return before != after


def _artifact_path(artifacts_dir: str | Path, name: str) -> Path:
    path = Path(artifacts_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _safe_send_photo_with_keyboard(tg, photo_path: Path, caption: str, keyboard: dict) -> list[tuple[str, int]]:
    return tg.send_photo_with_keyboard(photo_path, caption, keyboard)


def wait_for_login_or_back(
    page,
    tg,
    photo_path: str | Path,
    caption: str,
    current_offset,
    auth_runtime: dict,
    is_qr: bool = False,
    baseline_auth_fp: dict | None = None,
):
    keyboard = {"inline_keyboard": []}
    if is_qr:
        keyboard["inline_keyboard"].append(
            [{"text": "🔄 Я отсканировал", "callback_data": "auth_scanned"}]
        )
    keyboard["inline_keyboard"].append(
        [{"text": "🔙 Назад к выбору", "callback_data": "auth_back"}]
    )

    msg_ids = _safe_send_photo_with_keyboard(tg, Path(photo_path), caption, keyboard)

    go_back = False
    manual_probe_requested = False
    last_auth_probe_ts = 0.0

    while not go_back:
        page = get_latest_tiktok_page(page.context, page)

        try:
            updates = tg.poll_updates(offset=current_offset, timeout=5)
            if updates:
                for update in updates:
                    current_offset = update["update_id"] + 1
                    if "callback_query" not in update:
                        continue

                    callback_query = update["callback_query"]
                    cb_id = callback_query["id"]
                    cb_data = callback_query["data"]
                    tg.answer_callback(cb_id)

                    if cb_data == "auth_back":
                        go_back = True
                        break
                    if is_qr and cb_data == "auth_scanned":
                        auth_logger.info("Пользователь подтвердил, что QR отсканирован.")
                        manual_probe_requested = True
        except Exception:
            pass

        if go_back:
            break

        if is_qr and auth_runtime.get("qr_error_code") == 7:
            now_ts = time.time()
            qr_opened_ts = auth_runtime.get("qr_opened_ts")
            qr_open_age = now_ts - qr_opened_ts if qr_opened_ts is not None else 0
            auth_logger.warning(
                "QR-вход пока отклоняется TikTok: %s | qr_open_age=%.1fs",
                auth_runtime.get("qr_error_text"),
                qr_open_age,
            )

            if qr_open_age >= 120:
                if not auth_runtime.get("qr_error_notified"):
                    tg.send_text(
                        "❌ QR-вход временно недоступен: TikTok слишком долго возвращает ошибку.\n"
                        "Выбери вход по логину/паролю или ручной вход."
                    )
                    auth_runtime["qr_error_notified"] = True
                try:
                    page.goto("about:blank", wait_until="load", timeout=5000)
                except Exception:
                    pass
                break

            time.sleep(1)
            continue

        if is_logged_in(page):
            logger.info("✅ Вход подтверждён по UI.")
            break

        if is_qr and baseline_auth_fp is not None and (time.time() - last_auth_probe_ts >= 2.0):
            last_auth_probe_ts = time.time()
            current_auth_fp = get_tiktok_auth_fingerprint(page)
            if auth_fingerprint_changed(baseline_auth_fp, current_auth_fp):
                auth_logger.info("✅ Обнаружено изменение auth-state после QR. Переходим в messages...")
                baseline_auth_fp = current_auth_fp
                try:
                    page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2000)
                except Exception as exc:
                    auth_logger.warning("Не удалось открыть messages после изменения auth-state: %s", exc)

                if is_logged_in(page):
                    logger.info("✅ Вход подтверждён после перехода в messages.")
                    break

        if is_qr and manual_probe_requested:
            manual_probe_requested = False
            auth_logger.info("Даём TikTok 3 секунды на фиксацию сессии после сканирования...")
            page.wait_for_timeout(3000)
            current_auth_fp = get_tiktok_auth_fingerprint(page)
            if auth_fingerprint_changed(baseline_auth_fp, current_auth_fp):
                auth_logger.info("✅ После нажатия 'Я отсканировал' auth-state изменился.")
                baseline_auth_fp = current_auth_fp
                try:
                    page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2000)
                except Exception as exc:
                    auth_logger.warning("Не удалось открыть messages после ручной проверки: %s", exc)

                if is_logged_in(page):
                    logger.info("✅ Вход подтверждён после ручной проверки.")
                    break
            else:
                auth_logger.info("Auth-state ещё не изменился. QR оставляю открытым, продолжаю ждать.")

        time.sleep(1)

    for chat_id, msg_id in msg_ids:
        tg.clear_markup(chat_id, msg_id)
    return current_offset


def _initial_update_offset(tg):
    try:
        updates = tg.poll_updates(offset=None, timeout=1)
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return None


def _take_screenshot(page, artifacts_dir: str | Path, filename: str) -> Path:
    path = _artifact_path(artifacts_dir, filename)
    page.screenshot(path=str(path))
    return path


def _cleanup_telegram_markup(tg, msg_ids: list[tuple[str, int]]) -> None:
    for chat_id, msg_id in msg_ids:
        tg.clear_markup(chat_id, msg_id)


def _handle_qr_login(page, tg, offset, auth_runtime: dict, artifacts_dir: str | Path):
    reset_qr_runtime(auth_runtime)
    qr_btn = page.get_by_text(re.compile(r"QR", re.IGNORECASE)).first
    try:
        qr_btn.wait_for(state="attached", timeout=10000)
    except Exception:
        tg.send_text("❌ Кнопка QR не найдена.")
        return offset

    try:
        qr_btn.click()
    except Exception:
        try:
            qr_btn.evaluate("node => node.click()")
        except Exception:
            tg.send_text("❌ Не удалось нажать кнопку QR.")
            return offset

    auth_runtime["qr_opened_ts"] = time.time()
    dump_auth_state(page, "QR_SCREEN_OPENED")
    page.wait_for_timeout(3000)

    try:
        page.locator("canvas").last.wait_for(state="visible", timeout=10000)
    except Exception:
        pass

    qr_path = _artifact_path(artifacts_dir, "auth_qr.png")
    try:
        qr_element = page.locator("canvas").last
        if qr_element.is_visible():
            qr_element.screenshot(path=str(qr_path))
        else:
            page.screenshot(path=str(qr_path))
    except Exception:
        page.screenshot(path=str(qr_path))

    caption = (
        "✅ Отсканируй QR-код и подтверди вход на телефоне.\n\n"
        "Если TikTok не перекинул браузер сам, нажми кнопку «🔄 Я отсканировал»."
    )
    baseline_auth_fp = get_tiktok_auth_fingerprint(page)
    offset = wait_for_login_or_back(
        page=page,
        tg=tg,
        photo_path=qr_path,
        caption=caption,
        current_offset=offset,
        auth_runtime=auth_runtime,
        is_qr=True,
        baseline_auth_fp=baseline_auth_fp,
    )

    page = get_latest_tiktok_page(page.context, page)
    dump_auth_state(page, "AFTER_QR_WAIT")
    auth_logger.info("После QR-ожидания: url=%s, logged_in=%s", page.url, is_logged_in(page))

    if auth_runtime.get("qr_error_code") == 7:
        qr_opened_ts = auth_runtime.get("qr_opened_ts")
        qr_open_age = time.time() - qr_opened_ts if qr_opened_ts is not None else 0
        if qr_open_age >= 120:
            auth_logger.warning("QR действительно долго висит в ошибке. Возвращаю к выбору способа входа.")
            try:
                page.goto(
                    "https://www.tiktok.com/login?lang=ru-RU&redirect_url=https%3A%2F%2Fwww.tiktok.com%2Fmessages",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
            except Exception:
                pass
            page.wait_for_timeout(1500)

    if is_logged_in(page) and "tiktok.com/messages" not in page.url:
        try:
            page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass

    return offset


def _handle_password_login(page, tg, offset, auth_runtime: dict, artifacts_dir: str | Path):
    pass_btn = page.get_by_text(
        re.compile(r"телефон|почту|имя пользователя|phone|email|username", re.IGNORECASE)
    ).first
    if not pass_btn:
        tg.send_text("❌ Не удалось найти кнопку входа по логину.")
        return offset

    logger.info("Переходим к форме логина...")
    pass_btn.click()
    page.wait_for_timeout(2000)

    login_with_password_link = page.get_by_text(
        re.compile(r"Войти с паролем|Log in with password", re.IGNORECASE)
    ).first
    if login_with_password_link.is_visible():
        logger.info("Нажимаем 'Войти с паролем'...")
        login_with_password_link.click()
        page.wait_for_timeout(1500)

    region_path = _take_screenshot(page, artifacts_dir, "auth_region.png")
    keyboard_region = {"inline_keyboard": [
        [{"text": "➡️ Пропустить (регион уже верный)", "callback_data": "skip_region"}],
        [{"text": "🔙 Назад к выбору", "callback_data": "auth_back"}],
    ]}
    msg_ids_region = tg.send_photo_with_keyboard(
        region_path,
        "🌍 Отправь код страны ПРЯМО В ЧАТ (например: +7, +375, +48)\nИЛИ нажми 'Пропустить'.",
        keyboard_region,
    )

    region_choice = None
    user_text = None
    logger.info("Ждем ввода региона или нажатия кнопки...")

    while region_choice is None and user_text is None and not is_logged_in(page):
        try:
            updates = tg.poll_updates(offset=offset, timeout=5)
            if updates:
                for update in updates:
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        region_choice = update["callback_query"]["data"]
                        tg.answer_callback(update["callback_query"]["id"])
                    elif "message" in update and "text" in update["message"]:
                        user_text = update["message"]["text"].strip()
        except Exception:
            pass
        time.sleep(1)

    _cleanup_telegram_markup(tg, msg_ids_region)

    if region_choice == "auth_back":
        page.goto("https://www.tiktok.com/messages", wait_until="networkidle")
        return offset

    if user_text:
        logger.info("Вводим регион: %s", user_text)
        phone_input = page.get_by_placeholder(
            re.compile(r"Номер телефона|Phone number", re.IGNORECASE)
        ).first
        if phone_input.is_visible():
            box = phone_input.bounding_box()
            if box:
                page.mouse.click(box["x"] - 30, box["y"] + box["height"] / 2)
                page.wait_for_timeout(1000)
                search_input = page.get_by_placeholder(re.compile(r"Поиск|Search", re.IGNORECASE)).first
                if search_input.is_visible():
                    search_input.fill("")
                    search_input.type(user_text, delay=100)
                    page.wait_for_timeout(1500)
                    try:
                        target_item = page.locator("div, li, span").get_by_text(user_text, exact=False).filter(visible=True).last
                        if target_item.is_visible():
                            logger.info("Найдено совпадение для '%s', кликаем...", user_text)
                            target_item.click()
                        else:
                            page.keyboard.press("Enter")
                    except Exception:
                        page.keyboard.press("Enter")
                    page.wait_for_timeout(1000)

    final_path = _take_screenshot(page, artifacts_dir, "auth_pass_final.png")
    return wait_for_login_or_back(
        page=page,
        tg=tg,
        photo_path=final_path,
        caption="✅ Форма готова! Введи свой номер телефона и пароль в браузере.",
        current_offset=offset,
        auth_runtime=auth_runtime,
    )


def interactive_auth(page, tg, auth_runtime: dict, artifacts_dir: str | Path) -> None:
    offset = _initial_update_offset(tg)

    while not is_logged_in(page):
        page.wait_for_timeout(4000)
        auth_options_path = _take_screenshot(page, artifacts_dir, "auth_options.png")
        keyboard = {"inline_keyboard": [
            [{"text": "📱 Вход по QR", "callback_data": "auth_qr"}],
            [{"text": "🔑 Вход по Логину/Паролю", "callback_data": "auth_pass"}],
            [{"text": "💻 Сделаю всё сам", "callback_data": "auth_manual"}],
        ]}
        msg_ids = tg.send_photo_with_keyboard(auth_options_path, "Как будем входить в аккаунт?", keyboard)

        choice = None
        logger.info("Ждем выбор в Telegram...")
        while choice is None and not is_logged_in(page):
            try:
                updates = tg.poll_updates(offset=offset, timeout=5)
                if updates:
                    for update in updates:
                        offset = update["update_id"] + 1
                        if "callback_query" in update:
                            choice = update["callback_query"]["data"]
                            tg.answer_callback(update["callback_query"]["id"])
            except Exception:
                pass
            time.sleep(1)

        if is_logged_in(page):
            break

        _cleanup_telegram_markup(tg, msg_ids)

        if choice == "auth_qr":
            offset = _handle_qr_login(page, tg, offset, auth_runtime, artifacts_dir)
        elif choice == "auth_pass":
            offset = _handle_password_login(page, tg, offset, auth_runtime, artifacts_dir)
        elif choice == "auth_manual":
            tg.send_text("💻 Жду, пока ты развернешь окно и войдешь в аккаунт...")
            while not is_logged_in(page):
                time.sleep(3)

        if not is_logged_in(page):
            page.goto("https://www.tiktok.com/messages", wait_until="networkidle")
        elif "tiktok.com/messages" not in page.url:
            logger.info("Вход подтвержден, перехожу в раздел сообщений...")
            page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded")


def handle_captcha(page, tg, artifacts_dir: str | Path) -> bool:
    captcha_selectors = [
        '#captcha-verify-image',
        '.captcha-disable-scroll',
        'div[id^="secsdk-captcha"]',
        '#arkose-iframe',
        'iframe[src*="captcha"]',
    ]
    captcha_found = False
    active_selector = None

    for selector in captcha_selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=1000):
                captcha_found = True
                active_selector = selector
                break
        except Exception:
            continue

    if captcha_found:
        logger.warning("🚨 Обнаружена капча TikTok!")
        captcha_path = _take_screenshot(page, artifacts_dir, "captcha_alert.png")
        tg.send_photo(captcha_path)
        tg.send_text(
            "🚨 Внимание! TikTok выкинул капчу.\n"
            "Разверни браузер и реши её руками. Скрипт ждет..."
        )

        logger.info("Жду ручного решения капчи...")
        while True:
            try:
                if not page.locator(active_selector).first.is_visible(timeout=1000):
                    break
            except Exception:
                break
            time.sleep(2)

        logger.info("✅ Капча решена, продолжаем!")
        tg.send_text("✅ Отлично, капча пройдена! Бот продолжает работу.")
        time.sleep(2)
        return True

    return False

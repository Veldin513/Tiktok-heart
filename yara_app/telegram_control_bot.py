from __future__ import annotations

import atexit
import copy
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yara_app.fileio import atomic_write_text, file_lock, read_json
from yara_app.https import verified_context
from yara_app.project_adapter import ProjectAdapter
from yara_app.redaction import RedactingFormatter
from yara_app.telegram_errors import TelegramAPIError
from yara_app.telegram_login import TelegramLoginControls
from yara_app.telegram_profiles import PersonalProfileControls
from yara_app.telegram_setup import TelegramSetup, parse_chat_ids

CONFIG_NAME = "telegram_bot_v2.json"
LOCK_NAME = "telegram_bot_v2.lock"


@dataclass(slots=True)
class BotSettings:
    token: str
    allowed_chat_ids: set[int]
    poll_timeout: int = 50
    connect_timeout: int = 65
    page_size: int = 6
    state_file: str = "control/telegram_bot_v2_state.json"
    admin_name: str = "Admin"
    admin_chat_ids: set[int] | None = None
    profile_owners: dict[str, list[int]] = field(default_factory=dict)


class TelegramAPI:
    def __init__(self, token: str, timeout: int) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + method,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout, context=verified_context()) as response:
                data = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(65536))
                description = str(error.get('description') or exc.reason)
            except (ValueError, OSError, AttributeError):
                description = str(exc.reason)
            if method == 'getUpdates':
                raise  # Keep polling's conflict/backoff handling.
            raise TelegramAPIError(method, exc.code, description) from None
        if not data.get("ok"):
            raise TelegramAPIError(method, int(data.get('error_code') or 400), str(data.get('description') or 'Request rejected'))
        return data["result"]

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageText", payload)

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload)

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15)


class MenuBuilder:
    @staticmethod
    def inline(button_rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
        keyboard: list[list[dict[str, str]]] = []
        for row in button_rows:
            keyboard.append([{"text": text, "callback_data": callback} for text, callback in row])
        return {"inline_keyboard": keyboard}

    @classmethod
    def main_inline(cls, *, admin: bool = True) -> dict[str, Any]:
        rows = [[("👤 Все профили" if admin else "👤 Мой профиль", "nav:recipients")],
                [("📊 Статус", "nav:status"), ("🆔 Мой ID", "nav:myid")]]
        if admin:
            rows += [[("🛠 Общее управление", "nav:control")],
                     [("💬 Сообщения", "nav:messages"), ("🧪 Диагностика", "nav:diag")],
                     [("🔑 Пользователи и права", "nav:access")]]
        rows.append([('❔ Как пользоваться', 'nav:help')])
        return cls.inline(rows)

    @classmethod
    def control(cls, worker_running: bool, paused: bool) -> dict[str, Any]:
        worker_button = ("⏹ Остановить" if worker_running else "▶ Запустить", "act:worker:toggle")
        pause_button = ("▶ Снять паузу" if paused else "⏸ Пауза", "act:pause:toggle")
        return cls.inline(
            [
                [worker_button, ("🔁 Перезапуск", "act:worker:restart")],
                [pause_button, ("📊 Статус", "nav:status")],
                [("⬅ Назад", "nav:main")],
            ]
        )

    @classmethod
    def profiles(cls, profiles_text_chunk: list[tuple[int, str, bool]], page: int, total_pages: int) -> dict[str, Any]:
        rows: list[list[tuple[str, str]]] = []
        for index, label, enabled in profiles_text_chunk:
            icon = "🟢" if enabled else "⚪"
            rows.append([(f"{icon} {label[:28]}", f"act:profile:toggle:{index}")])

        pager: list[tuple[str, str]] = []
        if page > 0:
            pager.append(("⬅", f"nav:profiles:{page - 1}"))
        pager.append((f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            pager.append(("➡", f"nav:profiles:{page + 1}"))
        rows.append(pager)
        rows.append([("⬅ Назад", "nav:main"), ("♻ Обновить", f"nav:profiles:{page}")])
        return cls.inline(rows)

    @classmethod
    def secondary(cls) -> dict[str, Any]:
        return cls.inline([[ ("⬅ Назад", "nav:main"), ("♻ Обновить", "nav:refresh") ]])


class TelegramControlBot(TelegramLoginControls, PersonalProfileControls):
    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.adapter = ProjectAdapter(base_dir)
        self.settings = self._load_settings()
        self.api = TelegramAPI(self.settings.token, self.settings.connect_timeout)
        self.state_path = self.adapter.base_dir / 'control' / 'telegram_bot_v2_state.json'
        self.lock_path = self.adapter.control_dir / LOCK_NAME
        self.instance_pid = os.getpid()
        self._lock_acquired = False
        self._instance_lease = None
        self.offset, self.known_chat_ids = self._load_state()
        self.pending_profile_inputs = {}
        self.pending_access_inputs = {}
        self.access_confirmations = {}
        self.logger = self._configure_logger()
        self._init_login()

    def _configure_logger(self) -> logging.Logger:
        logger = logging.getLogger("telegram_control_bot_v2")
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        path = self.adapter.telegram_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        return logger

    def _load_settings(self) -> BotSettings:
        path = self.adapter.control_dir / CONFIG_NAME
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")

        payload = read_json(path)
        token = str(payload.get("token") or "").strip()
        if not token or token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
            raise ValueError("Не заполнен token в control/telegram_bot_v2.json")

        allowed = set(parse_chat_ids(payload.get("allowed_chat_ids", [])))
        if not allowed:
            raise ValueError('Укажите allowed_chat_ids: без списка владельцев запуск Telegram закрыт.')
        admins = set(parse_chat_ids(payload.get('admin_chat_ids', list(allowed))))
        if not admins or any(item <= 0 for item in admins):
            raise ValueError('Нужен хотя бы один ID администратора из личного чата.')
        owners = payload.get('profile_owners') or {}
        if not isinstance(owners, dict):
            raise ValueError('Некорректная привязка пользователей к профилям.')
        owners = {str(key): parse_chat_ids(ids) for key, ids in owners.items()}
        return BotSettings(
            token=token,
            allowed_chat_ids=allowed | admins,
            admin_chat_ids=admins,
            profile_owners=owners,
            poll_timeout=int(payload.get("poll_timeout", 50)),
            connect_timeout=int(payload.get("connect_timeout", 65)),
            page_size=int(payload.get("page_size", 6)),
            state_file=str(payload.get("state_file", "control/telegram_bot_v2_state.json")),
            admin_name=str(payload.get("admin_name", "Admin")),
        )

    def _load_state(self) -> tuple[int | None, set[int]]:
        payload = read_json(self.state_path)
        if payload is None:
            return None, set()
        if not isinstance(payload, dict):
            raise ValueError('Состояние Telegram повреждено. Запуск остановлен, чтобы не повторить старые команды.')
        fingerprint = hashlib.sha256(self.settings.token.encode()).hexdigest()
        if payload.get('token_fingerprint') not in (None, fingerprint):
            return None, set()
        offset = payload.get("offset")
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError('Некорректный offset Telegram. Исходный файл сохранён.')
        chats = set(parse_chat_ids(payload.get('known_chat_ids', [])))
        return offset, chats

    def _try_create_lock_file(self) -> bool:
        lease = file_lock(self.lock_path, timeout=0)
        try:
            lease.__enter__()
        except TimeoutError:
            return False
        existing_pid = self.adapter._read_pid_file(self.lock_path)
        launcher = (existing_pid == os.getppid() and self.adapter._pid_matches_script(
            existing_pid, self.adapter.telegram_script_path)) if existing_pid else False
        if existing_pid and existing_pid != self.instance_pid and self.adapter._pid_exists(existing_pid) and not launcher:
            lease.__exit__(None, None, None)
            return False
        try:
            atomic_write_text(self.lock_path, json.dumps({'pid': self.instance_pid, 'updated_at': time.time()}))
        except Exception:
            lease.__exit__(None, None, None)
            raise
        self._instance_lease = lease
        return True

    def _acquire_instance_lock(self) -> bool:
        if not self._try_create_lock_file():
            return False
        try:
            self.adapter.update_control_state({"telegram_bot_pid": self.instance_pid, "telegram_bot_started_at": time.time()})
        except Exception:
            self._instance_lease.__exit__(None, None, None)
            self._instance_lease = None
            raise
        self._lock_acquired = True
        return True

    def _release_instance_lock(self) -> None:
        if not self._lock_acquired:
            return
        try:
            current_pid = self.adapter._read_pid_file(self.lock_path)
            if current_pid == self.instance_pid:
                self.adapter._clear_pid_file(self.lock_path)
            state = self.adapter.get_control_state()
            if state.get("telegram_bot_pid") == self.instance_pid:
                self.adapter.update_control_state({"telegram_bot_pid": None, "telegram_bot_started_at": None})
        finally:
            self._lock_acquired = False
            if self._instance_lease is not None:
                self._instance_lease.__exit__(None, None, None)
                self._instance_lease = None

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "offset": self.offset,
            "known_chat_ids": sorted(self.known_chat_ids),
            'token_fingerprint': hashlib.sha256(self.settings.token.encode()).hexdigest(),
        }
        atomic_write_text(self.state_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _authorized(self, chat_id: int) -> bool:
        return chat_id in self.settings.allowed_chat_ids

    def _remember_chat(self, chat_id: int) -> None:
        if chat_id not in self.known_chat_ids:
            self.known_chat_ids.add(chat_id)
            self._save_state()

    def _restore_reply_panels(self) -> None:
        """Send main inline menu to all known chats on startup."""
        chat_ids = set(self.settings.allowed_chat_ids)
        for chat_id in sorted(chat_ids):
            try:
                self.api.send_message(
                    chat_id,
                    self._render_main_text(chat_id),
                    MenuBuilder.main_inline(admin=self._is_admin(chat_id)),
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Unable to restore panel for %s: %s", chat_id, exc)

    def _safe_edit_or_send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any],
        message_id: int | None = None,
    ) -> None:
        reply_markup = copy.deepcopy(reply_markup)
        rows = reply_markup.get('inline_keyboard', [])
        if rows and rows[-1] != [{'text': '🏠 Главное меню', 'callback_data': 'nav:main'}]:
            rows.append([{'text': '🏠 Главное меню', 'callback_data': 'nav:main'}])
        if message_id is None:
            self.api.send_message(chat_id, text, reply_markup)
            return
        try:
            self.api.edit_message_text(chat_id, message_id, text, reply_markup)
        except TelegramAPIError as exc:
            if exc.unchanged:
                return
            if exc.code != 400:
                raise
            self.api.send_message(chat_id, text, reply_markup)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            self.api.send_message(chat_id, text, reply_markup)

    def _render_main_text(self, chat_id: int) -> str:
        role = 'Администратор: доступны все профили и общие настройки.' if self._is_admin(chat_id) else 'Вам доступны только ваши личные профили.'
        return 'TikTok Heart\n' + role + '\n\nОткройте профиль, чтобы войти в TikTok, выбрать получателя или отправить сообщение.'

    def _send_main_entry(self, chat_id: int) -> None:
        self._remember_chat(chat_id)
        self._clear_pending(chat_id)
        self.api.send_message(chat_id, self._render_main_text(chat_id), MenuBuilder.main_inline(admin=self._is_admin(chat_id)))

    def _send_profiles(self, chat_id: int, message_id: int | None = None, page: int = 0) -> None:
        self._send_profile_list(chat_id, message_id, page)

    def _send_control(self, chat_id: int, message_id: int | None = None) -> None:
        state = self.adapter.get_control_state()
        worker = self.adapter.get_worker_status()
        self._safe_edit_or_send(
            chat_id,
            self._view_status(chat_id),
            MenuBuilder.control(worker.running, bool(state.get("paused"))),
            message_id,
        )

    def _handle_text_action(self, chat_id: int, text: str) -> bool:
        """Handle slash commands only — reply keyboard removed, inline only."""
        normalized = text.strip()
        if normalized in {'/start', '/menu', '/help', '/status', '/profiles', '/control', '/diag'}:
            self._clear_pending(chat_id)
        if normalized in {"/start", "/menu", "/help"}:
            self._send_main_entry(chat_id)
            return True
        if normalized in {"/status"}:
            self.api.send_message(chat_id, self._view_status(chat_id), MenuBuilder.secondary())
            return True
        if normalized in {"/profiles"}:
            self._send_profiles(chat_id)
            return True
        if normalized in {"/control"}:
            self._send_control(chat_id)
            return True
        if normalized in {"/diag"}:
            self.api.send_message(chat_id, self.adapter.render_diagnostics_text(), MenuBuilder.secondary())
            return True
        return False

    def _handle_command(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        if message['chat'].get('type', 'private') != 'private':
            return
        text = str(message.get('text') or '').strip()
        command = text.split(' ', 1)[0].split('@', 1)[0].lower()
        if command in {'/cancel', '/start', '/menu', '/help', '/status', '/id', '/profiles', '/recipients',
                       '/myprofile', '/control', '/diag', '/access', '/user', '/admin', '/revoke'}:
            self.login.cancel(chat_id)
        elif self._handle_login_input(message):
            return
        if command == '/id' or text == '🆔 Мой ID':
            self._send_id(chat_id)
            return
        if not self._authorized(chat_id):
            self.api.send_message(chat_id, f'Ваш Telegram ID: {chat_id}. Передайте его администратору для доступа.')
            return
        self._ensure_user_profile(message)
        self._remember_chat(chat_id)
        if command == '/cancel':
            self._clear_pending(chat_id)
            self._safe_edit_or_send(chat_id, 'Изменение отменено.', MenuBuilder.main_inline(admin=self._is_admin(chat_id)))
            return
        if not text.startswith('/') and self._handle_access_input(chat_id, text):
            return
        if not text.startswith('/') and self._handle_profile_input(chat_id, text):
            return
        if command in {'/user', '/admin', '/revoke'}:
            if not self._is_admin(chat_id):
                raise PermissionError('Доступ меняет только администратор.')
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                raise ValueError(f'Укажите команду и ID, например {command} 123456789.')
            TelegramSetup(self.adapter).change_access(chat_id, int(parts[1]), command[1:])
            self.settings = self._load_settings()
            self.api.send_message(chat_id, 'Права сохранены. Пользователь может отправить /start.')
            return
        if command in {'/myprofile', '/recipients'}:
            self._send_profile_list(chat_id)
            return
        if command == '/access':
            self._send_access(chat_id)
            return
        if not self._is_admin(chat_id) and command not in {'/start', '/menu', '/help', '/status'}:
            self._send_profile_list(chat_id)
            return
        if self._handle_text_action(chat_id, command):
            return
        self._send_main_entry(chat_id)

    def _handle_navigation(self, chat_id: int, message_id: int, command: str) -> None:
        parts = command.split(":")
        route = parts[1]
        self.login.cancel(chat_id)
        self._clear_pending(chat_id)

        if route == 'help':
            self._safe_edit_or_send(chat_id,
                '1. Откройте свой профиль.\n2. Нажмите «Войти» и выберите QR или пароль.\n'
                '3. Укажите получателя и нажмите «Запустить отправку».\n\n'
                'Если TikTok попросит код, пришлите его в этот чат.', MenuBuilder.secondary(), message_id)
            return

        if route == 'myid':
            self._send_id(chat_id)
            return
        if route == 'recipients':
            self._send_profile_list(chat_id, message_id, int(parts[2]) if len(parts) > 2 else 0)
            return
        if route == 'access':
            self._send_access(chat_id, message_id)
            return
        if not self._is_admin(chat_id) and route not in {'main', 'status', 'refresh'}:
            raise PermissionError('Общие настройки доступны только администратору.')
        if route == "main":
            self._safe_edit_or_send(chat_id, self._render_main_text(chat_id), MenuBuilder.main_inline(admin=self._is_admin(chat_id)), message_id)
            return
        if route in {"refresh", "status"}:
            self._safe_edit_or_send(chat_id, self._view_status(chat_id), MenuBuilder.secondary(), message_id)
            return
        if route == "messages":
            self._safe_edit_or_send(chat_id, self.adapter.render_messages_text(), MenuBuilder.secondary(), message_id)
            return
        if route == "diag":
            self._safe_edit_or_send(chat_id, self.adapter.render_diagnostics_text(), MenuBuilder.secondary(), message_id)
            return
        if route == "control":
            self._send_control(chat_id, message_id)
            return
        if route == "profiles":
            page = int(parts[2]) if len(parts) > 2 else 0
            self._send_profiles(chat_id, message_id, page)
            return

    def _handle_action(self, chat_id: int, message_id: int, command: str) -> None:
        if not self._is_admin(chat_id):
            raise PermissionError('Это действие доступно только администратору.')
        parts = command.split(":")
        target = parts[1]

        if target == "worker":
            op = parts[2]
            if op == "toggle":
                worker = self.adapter.get_worker_status()
                if worker.running:
                    self.adapter.stop_worker()
                else:
                    self.adapter.start_profile_worker(self.adapter.get_control_state()['active_profile'], ignore_cooldown=True)
            elif op == "restart":
                self.adapter.stop_worker()
                self.adapter.start_profile_worker(self.adapter.get_control_state()['active_profile'], ignore_cooldown=True)
            self._send_control(chat_id, message_id)
            return

        if target == "pause":
            state = self.adapter.get_control_state()
            paused = not bool(state.get("paused"))
            self.adapter.update_control_state({"paused": paused})
            self._send_control(chat_id, message_id)
            return

        if target == "profile":
            # Legacy buttons use list indexes, which can now point to another
            # profile. Ask the administrator to select a stable profile button.
            self._send_profile_list(chat_id, message_id)
            return

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback["id"]
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if not chat.get('id') or not message.get('message_id'):
            return
        chat_id = int(chat['id'])
        message_id = int(message['message_id'])
        data = str(callback.get("data") or "")
        actor_id = (callback.get('from') or {}).get('id')
        if actor_id is not None and int(actor_id) != chat_id:
            return
        if chat.get('type', 'private') != 'private':
            self._answer_callback(query_id, 'Откройте личный чат с ботом.')
            return
        if data == 'nav:myid':
            self._answer_callback(query_id)
            self._send_id(chat_id)
            return

        if not self._authorized(chat_id):
            self._answer_callback(query_id, "Доступ запрещён")
            return

        self._remember_chat(chat_id)
        fresh = self._answer_callback(query_id)
        if not fresh and (data.startswith(('act:', 'access:', 'auth:')) or
                          (data.startswith('p:') and data.split(':')[2] != 'open')):
            self._safe_edit_or_send(chat_id, 'Эта кнопка нажата слишком давно. Выберите действие ещё раз.',
                                    MenuBuilder.main_inline(admin=self._is_admin(chat_id)), message_id)
            return

        if data == "noop":
            return
        if data == 'input:cancel':
            self.login.cancel(chat_id)
            self._clear_pending(chat_id)
            self._safe_edit_or_send(chat_id, 'Изменение отменено.', MenuBuilder.main_inline(admin=self._is_admin(chat_id)), message_id)
            return
        if data.startswith('access:'):
            self._handle_access_callback(chat_id, message_id, data)
            return
        if data.startswith('auth:'):
            self._handle_login_callback(chat_id, data)
            return
        if data.startswith('p:'):
            self._handle_profile_callback(chat_id, message_id, data)
            return
        if data.startswith("nav:"):
            self._handle_navigation(chat_id, message_id, data)
            return
        if data.startswith("act:"):
            self._handle_action(chat_id, message_id, data)
            return
        self._safe_edit_or_send(chat_id, 'Меню обновилось. Выберите нужный пункт.',
                                MenuBuilder.main_inline(admin=self._is_admin(chat_id)), message_id)

    def _answer_callback(self, query_id: str, text: str | None = None) -> bool:
        try:
            self.api.answer_callback(query_id, text)
            return True
        except (TelegramAPIError, urllib.error.URLError, OSError) as exc:
            # Telegram's spinner acknowledgement must not break navigation.
            self.logger.warning('Callback acknowledgement failed: %s', exc)
            return False

    def process_update(self, update: dict[str, Any]) -> None:
        self.settings = self._load_settings()
        message = update.get('message') or (update.get('callback_query') or {}).get('message') or {}
        chat_id = (message.get('chat') or {}).get('id')
        try:
            if isinstance(update.get('message'), dict) and message.get('text'):
                self._handle_command(message)
            elif isinstance(update.get('callback_query'), dict):
                self._handle_callback(update['callback_query'])
        except (ValueError, PermissionError, RuntimeError, OSError, KeyError, IndexError) as exc:
            self.logger.warning('Telegram action rejected: %s', exc)
            private = (message.get('chat') or {}).get('type', 'private') == 'private'
            if chat_id is not None and private:
                if isinstance(exc, TelegramAPIError):
                    text = exc.user_message()
                elif isinstance(exc, (KeyError, IndexError)):
                    text = 'Этот пункт уже изменился. Откройте профиль заново.'
                elif isinstance(exc, OSError) and not isinstance(exc, PermissionError):
                    text = 'Не удалось выполнить действие. Проверьте интернет и доступность приложения на компьютере.'
                else:
                    from yara_app.redaction import redact_secrets
                    text = redact_secrets(str(exc)).replace('worker', 'отправку').replace('Worker', 'Отправка')
                try:
                    self.api.send_message(int(chat_id), '⚠ ' + text, MenuBuilder.inline([
                        [('↩ Отменить ввод', 'input:cancel'), ('🏠 Главное меню', 'nav:main')]]))
                except (RuntimeError, OSError) as send_error:
                    self.logger.warning('Unable to deliver error explanation: %s', send_error)

    def _consume_startup_updates(self, updates: list[dict]) -> None:
        """Coalesce old menu requests; never execute commands queued while offline."""
        latest = {}
        for update in updates:
            message = update.get('message')
            callback = update.get('callback_query')
            if callback:
                original = callback.get('message') or {}
                message = {'chat': original.get('chat') or {}, 'from': callback.get('from') or {}, 'text': '/start'}
            if isinstance(message, dict) and (message.get('chat') or {}).get('type') == 'private':
                latest[int(message['chat']['id'])] = message
            self.offset = max(self.offset or 0, int(update['update_id']) + 1)
        if updates:
            self._save_state()
        for message in latest.values():
            # Only /id remains a command here. Everything else becomes a menu
            # request so a stale worker/login/access button cannot fire on boot.
            command = str(message.get('text') or '').split(' ', 1)[0].lower()
            safe_message = {**message, 'text': '/id' if command == '/id' else '/start'}
            try:
                self.process_update({'message': safe_message})
            except Exception:
                self.logger.exception('Unable to restore a single startup menu')

    def _drain_startup_updates(self, first: list[dict]) -> list[dict]:
        """Telegram returns at most 100 updates: later backlog pages are stale too."""
        updates = list(first)
        batch = first
        while len(batch) >= 100:
            offset = max(int(item['update_id']) for item in batch) + 1
            batch = self.api.get_updates(offset, 0)
            updates.extend(batch)
        return updates

    def run_forever(self) -> None:
        if not self._acquire_instance_lock():
            return
        atexit.register(self._release_instance_lock)
        self.logger.info("Bot started")
        try:
            backoff = 2
            startup = True
            while True:
                try:
                    updates = self.api.get_updates(self.offset, 0 if startup else self.settings.poll_timeout)
                    if startup:
                        updates = self._drain_startup_updates(updates)
                        startup = False
                        self._consume_startup_updates(updates)
                        continue
                    for update in updates:
                        update_id = int(update["update_id"])
                        try:
                            self.process_update(update)
                        except Exception:
                            self.logger.exception('Update %s failed; it will not be replayed automatically', update_id)
                        self.offset = update_id + 1
                        self._save_state()
                    backoff = 2
                except urllib.error.HTTPError as exc:
                    if getattr(exc, "code", None) == 409:
                        self.logger.error("HTTP 409 Conflict: обнаружен конкурентный polling другого процесса. Экземпляр будет остановлен.")
                        break
                    self.logger.exception("HTTP error: %s", exc)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                except urllib.error.URLError as exc:
                    self.logger.exception("URL error: %s", exc)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                except KeyboardInterrupt:
                    self.logger.info("Bot interrupted by user")
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Unhandled error: %s", exc)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            self.login.close()
            self._release_instance_lock()


if __name__ == "__main__":
    TelegramControlBot().run_forever()

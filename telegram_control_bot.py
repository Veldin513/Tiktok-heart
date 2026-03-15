from __future__ import annotations

import atexit
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_adapter import ProjectAdapter


CONFIG_NAME = "telegram_bot_v2.json"
LOCK_NAME = "telegram_bot_v2.lock"


def parse_command(text: str) -> tuple[str, str]:
    """Split a Telegram command into the command token and the remaining text.

    The helper is intentionally tiny and dependency-free because it is used by
    tests and can also serve future command-based entry points. Empty input
    yields a pair of empty strings.
    """

    normalized = str(text or "").strip()
    if not normalized:
        return "", ""
    command, _, rest = normalized.partition(" ")
    return command, rest.strip()


@dataclass(slots=True)
class BotSettings:
    token: str
    allowed_chat_ids: set[int]
    poll_timeout: int = 50
    connect_timeout: int = 65
    page_size: int = 6
    state_file: str = "control/telegram_bot_v2_state.json"
    admin_name: str = "Admin"


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
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
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
    def main_inline(cls) -> dict[str, Any]:
        return cls.inline(
            [
                [("📊 Статус", "nav:status"), ("🎛 Профили", "nav:profiles:0")],
                [("💬 Сообщения", "nav:messages"), ("🛠 Управление", "nav:control")],
                [("🧪 Диагностика", "nav:diag"), ("♻ Обновить", "nav:refresh")],
            ]
        )

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


class TelegramControlBot:
    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.adapter = ProjectAdapter(base_dir)
        self.settings = self._load_settings()
        self.api = TelegramAPI(self.settings.token, self.settings.connect_timeout)
        self.state_path = self.adapter.base_dir / self.settings.state_file
        self.lock_path = self.adapter.control_dir / LOCK_NAME
        self.instance_pid = os.getpid()
        self._lock_acquired = False
        self.offset, self.known_chat_ids = self._load_state()
        self.logger = self._configure_logger()

    def _configure_logger(self) -> logging.Logger:
        logger = logging.getLogger("telegram_control_bot_v2")
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        path = self.adapter.telegram_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        return logger

    def _load_settings(self) -> BotSettings:
        path = self.adapter.control_dir / CONFIG_NAME
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")

        payload = json.loads(path.read_text(encoding="utf-8"))
        token = str(payload.get("token") or "").strip()
        if not token or token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
            raise ValueError("Не заполнен token в control/telegram_bot_v2.json")

        allowed = {int(item) for item in payload.get("allowed_chat_ids", [])}
        return BotSettings(
            token=token,
            allowed_chat_ids=allowed,
            poll_timeout=int(payload.get("poll_timeout", 50)),
            connect_timeout=int(payload.get("connect_timeout", 65)),
            page_size=int(payload.get("page_size", 6)),
            state_file=str(payload.get("state_file", "control/telegram_bot_v2_state.json")),
            admin_name=str(payload.get("admin_name", "Admin")),
        )

    def _load_state(self) -> tuple[int | None, set[int]]:
        if not self.state_path.exists():
            return None, set()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return None, set()
        offset = payload.get("offset")
        raw_chats = payload.get("known_chat_ids") or []
        chats: set[int] = set()
        for item in raw_chats:
            try:
                chats.add(int(item))
            except Exception:
                continue
        return (int(offset) if offset is not None else None), chats

    def _try_create_lock_file(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": self.instance_pid, "updated_at": time.time()}, ensure_ascii=False, indent=2)
        for _attempt in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                existing_pid = self.adapter._read_pid_file(self.lock_path)
                if existing_pid and existing_pid != self.instance_pid and self.adapter._pid_exists(existing_pid):
                    self.logger.error("Telegram bot already running with PID %s", existing_pid)
                    return False
                self.adapter._clear_pid_file(self.lock_path)
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise
            return True
        return False

    def _acquire_instance_lock(self) -> bool:
        if not self._try_create_lock_file():
            return False
        self.adapter.update_control_state({"telegram_bot_pid": self.instance_pid, "telegram_bot_started_at": time.time()})
        self._lock_acquired = True
        return True

    def _release_instance_lock(self) -> None:
        if not self._lock_acquired:
            return
        current_pid = self.adapter._read_pid_file(self.lock_path)
        if current_pid == self.instance_pid:
            self.adapter._clear_pid_file(self.lock_path)
        state = self.adapter.get_control_state()
        if state.get("telegram_bot_pid") == self.instance_pid:
            self.adapter.update_control_state({"telegram_bot_pid": None, "telegram_bot_started_at": None})
        self._lock_acquired = False

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "offset": self.offset,
            "known_chat_ids": sorted(self.known_chat_ids),
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _authorized(self, chat_id: int) -> bool:
        if not self.settings.allowed_chat_ids:
            return True
        return chat_id in self.settings.allowed_chat_ids

    def _remember_chat(self, chat_id: int) -> None:
        if chat_id not in self.known_chat_ids:
            self.known_chat_ids.add(chat_id)
            self._save_state()

    def _restore_reply_panels(self) -> None:
        """Send main inline menu to all known chats on startup."""
        chat_ids = set(self.known_chat_ids)
        chat_ids.update(self.settings.allowed_chat_ids)
        for chat_id in sorted(chat_ids):
            try:
                self.api.send_message(
                    chat_id,
                    self._render_main_text(),
                    MenuBuilder.main_inline(),
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
        if message_id is None:
            self.api.send_message(chat_id, text, reply_markup)
            return
        try:
            self.api.edit_message_text(chat_id, message_id, text, reply_markup)
        except Exception:  # noqa: BLE001
            self.api.send_message(chat_id, text, reply_markup)

    def _render_main_text(self) -> str:
        return (
            f"Панель управления TikTok-проектом\n"
            f"Администратор: {self.settings.admin_name}\n\n"
            f"Нижняя клавиатура закреплена в чате.\n"
            f"Для профилей и быстрых действий используйте кнопки внутри сообщений."
        )

    def _send_main_entry(self, chat_id: int) -> None:
        self._remember_chat(chat_id)
        self.api.send_message(chat_id, self._render_main_text(), MenuBuilder.main_inline())

    def _send_profiles(self, chat_id: int, message_id: int | None = None, page: int = 0) -> None:
        body, chunk, total_pages = self.adapter.render_profiles_page(page, self.settings.page_size)
        start = page * self.settings.page_size
        keyboard_chunk = [(start + idx, profile.label, profile.enabled) for idx, profile in enumerate(chunk)]
        self._safe_edit_or_send(chat_id, body, MenuBuilder.profiles(keyboard_chunk, page, total_pages), message_id)

    def _send_control(self, chat_id: int, message_id: int | None = None) -> None:
        state = self.adapter.get_control_state()
        worker = self.adapter.get_worker_status()
        self._safe_edit_or_send(
            chat_id,
            self.adapter.render_status_text(),
            MenuBuilder.control(worker.running, bool(state.get("paused"))),
            message_id,
        )

    def _handle_text_action(self, chat_id: int, text: str) -> bool:
        """Handle slash commands only — reply keyboard removed, inline only."""
        normalized = text.strip()
        if normalized in {"/start", "/menu", "/help"}:
            self._send_main_entry(chat_id)
            return True
        if normalized in {"/status"}:
            self.api.send_message(chat_id, self.adapter.render_status_text(), MenuBuilder.secondary())
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
        if not self._authorized(chat_id):
            self.api.send_message(chat_id, "Доступ запрещён.")
            return

        self._remember_chat(chat_id)
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if self._handle_text_action(chat_id, text):
            return
        self._send_main_entry(chat_id)

    def _handle_navigation(self, chat_id: int, message_id: int, command: str) -> None:
        parts = command.split(":")
        route = parts[1]

        if route == "main":
            self._safe_edit_or_send(chat_id, self._render_main_text(), MenuBuilder.main_inline(), message_id)
            return
        if route in {"refresh", "status"}:
            self._safe_edit_or_send(chat_id, self.adapter.render_status_text(), MenuBuilder.secondary(), message_id)
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
        parts = command.split(":")
        target = parts[1]

        if target == "worker":
            op = parts[2]
            if op == "toggle":
                worker = self.adapter.get_worker_status()
                if worker.running:
                    self.adapter.stop_worker()
                else:
                    self.adapter.start_worker()
            elif op == "restart":
                self.adapter.restart_worker()
            self._send_control(chat_id, message_id)
            return

        if target == "pause":
            state = self.adapter.get_control_state()
            paused = not bool(state.get("paused"))
            self.adapter.update_control_state({"paused": paused})
            self._send_control(chat_id, message_id)
            return

        if target == "profile":
            index = int(parts[3])
            self.adapter.toggle_profile(index)
            page = index // self.settings.page_size
            self._send_profiles(chat_id, message_id, page)
            return

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback["id"]
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat["id"])
        message_id = int(message["message_id"])
        data = str(callback.get("data") or "")

        if not self._authorized(chat_id):
            self.api.answer_callback(query_id, "Доступ запрещён")
            return

        self._remember_chat(chat_id)
        self.api.answer_callback(query_id)

        if data == "noop":
            return
        if data.startswith("nav:"):
            self._handle_navigation(chat_id, message_id, data)
            return
        if data.startswith("act:"):
            self._handle_action(chat_id, message_id, data)
            return

    def process_update(self, update: dict[str, Any]) -> None:
        if "message" in update and isinstance(update["message"], dict):
            message = update["message"]
            if message.get("text"):
                self._handle_command(message)
            return
        if "callback_query" in update and isinstance(update["callback_query"], dict):
            self._handle_callback(update["callback_query"])
            return

    def run_forever(self) -> None:
        if not self._acquire_instance_lock():
            return
        atexit.register(self._release_instance_lock)
        self.logger.info("Bot started")
        try:
            try:
                self._restore_reply_panels()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Reply panel restore skipped: %s", exc)
            backoff = 2
            while True:
                try:
                    updates = self.api.get_updates(self.offset, self.settings.poll_timeout)
                    for update in updates:
                        update_id = int(update["update_id"])
                        self.process_update(update)
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
            self._release_instance_lock()


if __name__ == "__main__":
    TelegramControlBot().run_forever()

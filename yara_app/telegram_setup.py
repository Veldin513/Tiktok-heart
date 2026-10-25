"""Desktop setup for a BotFather bot. Never returns stored credentials to the UI."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from yara_app.fileio import atomic_write_text, file_lock, read_json

TOKEN_PATTERN = re.compile(r"\d{5,16}:[A-Za-z0-9_-]{20,200}\Z")


def parse_chat_ids(value: Any) -> list[int]:
    if isinstance(value, str):
        value = re.split(r"[,;\s]+", value.strip()) if value.strip() else []
    if not isinstance(value, list):
        raise ValueError('Укажите Telegram ID числами через запятую или с новой строки.')
    result = set()
    for item in value:
        if isinstance(item, bool) or not re.fullmatch(r'-?\d{1,16}', str(item)) or int(item) == 0:
            raise ValueError('Telegram ID должен быть ненулевым целым числом.')
        result.add(int(item))
    return sorted(result)


class TelegramSetup:
    def __init__(self, adapter):
        self.adapter = adapter
        self.path = adapter.telegram_config_path

    def _read(self) -> dict:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            raise ValueError('Некорректный конфиг Telegram. Исходный файл сохранён.')
        return data

    def public_settings(self) -> dict:
        data = self._read()
        token = str(data.get('token') or '').strip()
        return {'token_configured': bool(TOKEN_PATTERN.fullmatch(token)),
                'allowed_chat_ids': parse_chat_ids(data.get('allowed_chat_ids', [])),
                'admin_name': str(data.get('admin_name') or 'Admin')}

    def _require_stopped(self):
        if self.adapter.get_telegram_bot_status().running:
            raise ValueError('Сначала остановите Telegram-бота кнопкой «Остановить» выше.')

    def save(self, payload: dict) -> dict:
        self._require_stopped()
        with file_lock(self.path):
            data = self._read()
            token = str(payload.get('token') or data.get('token') or '').strip()
            if not TOKEN_PATTERN.fullmatch(token):
                raise ValueError('Введите токен, полученный от BotFather.')
            ids = parse_chat_ids(payload.get('allowed_chat_ids', data.get('allowed_chat_ids', [])))
            if not ids:
                raise ValueError('Добавьте хотя бы один разрешённый Telegram ID. Без него доступ закрыт.')
            admin = str(payload.get('admin_name', data.get('admin_name', 'Admin'))).strip()
            if not 1 <= len(admin) <= 80:
                raise ValueError('Имя владельца должно содержать от 1 до 80 символов.')
            data.update(token=token, allowed_chat_ids=ids, admin_name=admin,
                        state_file='control/telegram_bot_v2_state.json')
            atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        return self.public_settings()

    def _call(self, method: str, payload: dict, token: str = ''):
        token = (token or str(self._read().get('token') or '')).strip()
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError('Сначала вставьте токен BotFather.')
        request = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/{method}',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            messages = {401: 'Токен не принят Telegram.', 404: 'Токен не принят Telegram.',
                        409: 'У бота включён webhook или запущен другой экземпляр polling. Остановите его.'}
            raise ValueError(messages.get(exc.code, f'Telegram вернул HTTP {exc.code}.')) from None
        except (OSError, ValueError):
            raise ValueError('Не удалось получить ответ Telegram. Проверьте соединение и повторите.') from None
        if not isinstance(data, dict) or not data.get('ok'):
            raise ValueError('Telegram отклонил запрос. Проверьте токен и настройки бота.')
        return data.get('result')

    def check(self, token: str = '') -> dict:
        user = self._call('getMe', {}, token)
        return {'username': str(user.get('username') or ''), 'name': str(user.get('first_name') or '')}

    def discover_chats(self, token: str = '') -> list[dict]:
        self._require_stopped()
        # No offset: preview updates without acknowledging them or running commands.
        updates = self._call('getUpdates', {'timeout': 0, 'limit': 100}, token)
        chats = {}
        for update in updates or []:
            message = update.get('message') or {}
            chat = message.get('chat') or {}
            if chat.get('type') != 'private' or str(message.get('text') or '').split(' ', 1)[0] != '/start':
                continue
            chat_id = int(chat['id'])
            chats[chat_id] = {'id': chat_id, 'name': str(chat.get('first_name') or ''),
                              'username': str(chat.get('username') or '')}
        return list(chats.values())

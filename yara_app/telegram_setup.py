"""Desktop setup for a BotFather bot. Never returns stored credentials to the UI."""
from __future__ import annotations

import json
import re
import secrets
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from yara_app.fileio import atomic_write_text, file_lock, read_json
from yara_app.https import verified_context

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
        allowed = parse_chat_ids(data.get('allowed_chat_ids', []))
        admins = parse_chat_ids(data.get('admin_chat_ids', allowed))
        return {'token_configured': bool(TOKEN_PATTERN.fullmatch(token)),
                'allowed_chat_ids': allowed, 'admin_chat_ids': admins,
                'profile_owners': data.get('profile_owners') or {},
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
            admins = parse_chat_ids(payload.get('admin_chat_ids', data.get('admin_chat_ids', ids)))
            if not admins or any(item <= 0 for item in admins):
                raise ValueError('Укажите хотя бы один положительный ID администратора из личного чата.')
            owners = data.get('profile_owners') or {}
            if 'profile_owners' in payload:
                raw_owners = payload['profile_owners']
                if not isinstance(raw_owners, dict):
                    raise ValueError('Некорректный список владельцев профилей.')
                keys = {profile.key for profile in self.adapter.get_profiles()}
                owners = {}
                for key, raw_ids in raw_owners.items():
                    if key not in keys:
                        raise ValueError('Выбранный профиль больше не существует. Обновите список.')
                    owner_ids = parse_chat_ids(raw_ids)
                    if any(item <= 0 for item in owner_ids):
                        raise ValueError('Владельцу профиля нужен ID из личного чата.')
                    if owner_ids:
                        owners[key] = owner_ids
            ids = sorted(set(ids) | set(admins))
            owners = {key: [item for item in parse_chat_ids(owner_ids) if item in ids]
                      for key, owner_ids in owners.items()}
            owners = {key: owner_ids for key, owner_ids in owners.items() if owner_ids}
            admin = str(payload.get('admin_name', data.get('admin_name', 'Admin'))).strip()
            if not 1 <= len(admin) <= 80:
                raise ValueError('Имя владельца должно содержать от 1 до 80 символов.')
            data.update(token=token, allowed_chat_ids=ids, admin_chat_ids=admins, profile_owners=owners, admin_name=admin,
                        state_file='control/telegram_bot_v2_state.json')
            atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        return self.public_settings()

    def _call(self, method: str, payload: dict, token: str = '', *, photo_path: Path | None = None):
        token = (token or str(self._read().get('token') or '')).strip()
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError('Сначала вставьте токен BotFather.')
        body = json.dumps(payload).encode()
        content_type = 'application/json'
        if photo_path is not None:
            if method != 'sendPhoto':
                raise ValueError('Вложение поддерживается только для отправки фото.')
            with Path(photo_path).open('rb') as photo:
                image = photo.read(8 * 1024 * 1024 + 1)
            if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 8 * 1024 * 1024:
                raise ValueError('Скриншот должен быть PNG размером до 8 МБ.')
            boundary = 'TikTokHeart' + secrets.token_hex(16)
            chunks = []
            for key, value in payload.items():
                if key not in {'chat_id', 'caption'}:
                    raise ValueError('Неизвестный параметр вложения.')
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="error.png"\r\nContent-Type: image/png\r\n\r\n'.encode(),
                       image, f'\r\n--{boundary}--\r\n'.encode()]
            body = b''.join(chunks)
            content_type = f'multipart/form-data; boundary={boundary}'
        request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
                                         data=body, headers={'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=10, context=verified_context()) as response:
                data = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            messages = {401: 'Токен не принят Telegram.', 404: 'Токен не принят Telegram.',
                        409: 'У бота включён webhook или запущен другой экземпляр polling. Остановите его.'}
            raise ValueError(messages.get(exc.code, f'Telegram вернул HTTP {exc.code}.')) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ValueError('Не удалось проверить сертификат Telegram. Обновите зависимости приложения и проверьте дату Windows.') from None
            raise ValueError('Не удалось подключиться к Telegram. Проверьте соединение и повторите.') from None
        except (OSError, ValueError):
            raise ValueError('Не удалось получить ответ Telegram. Проверьте соединение и повторите.') from None
        if not isinstance(data, dict) or not data.get('ok'):
            raise ValueError('Telegram отклонил запрос. Проверьте токен и настройки бота.')
        return data.get('result')

    def check(self, token: str = '') -> dict:
        user = self._call('getMe', {}, token)
        return {'username': str(user.get('username') or ''), 'name': str(user.get('first_name') or '')}

    def test_notification(self) -> dict:
        ids = parse_chat_ids(self._read().get('allowed_chat_ids', []))
        if not ids:
            raise ValueError('Сначала сохраните разрешённый Telegram ID.')
        delivered = 0
        for chat_id in ids:
            result = self._call('sendMessage', {'chat_id': chat_id, 'text':
                '🔧 Проверка TikTok Heart: связь с Telegram работает.\n'
                'Это тестовое уведомление. TikTok не запускался, сообщения в TikTok не отправлялись.'})
            if not isinstance(result, dict) or not result.get('message_id'):
                raise ValueError('Telegram не подтвердил доставку теста.')
            delivered += 1
        return {'delivered_count': delivered}

    def ensure_user_profile(self, user_id: int, username: str, display_name: str = ''):
        with file_lock(self.path):
            config = self._read()
            allowed = parse_chat_ids(config.get('allowed_chat_ids', []))
            admins = parse_chat_ids(config.get('admin_chat_ids', allowed))
            if user_id not in set(allowed) | set(admins):
                raise PermissionError('Администратор ещё не разрешил этому ID доступ.')
            profile = self.adapter.ensure_telegram_profile(user_id, username, display_name)
            owners = dict(config.get('profile_owners') or {})
            owners[profile.key] = sorted(set(parse_chat_ids(owners.get(profile.key, []))) | {user_id})
            config['profile_owners'] = owners
            config.setdefault('admin_chat_ids', admins)
            atomic_write_text(self.path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        return profile

    def change_access(self, actor_id: int, user_id: int, role: str) -> None:
        if user_id <= 0 or role not in {'admin', 'user', 'revoke'}:
            raise ValueError('Нужен положительный Telegram ID пользователя.')
        with file_lock(self.path):
            config = self._read()
            allowed = set(parse_chat_ids(config.get('allowed_chat_ids', [])))
            admins = set(parse_chat_ids(config.get('admin_chat_ids', list(allowed))))
            if actor_id not in admins:
                raise PermissionError('Доступ может менять только администратор.')
            if role == 'admin':
                admins.add(user_id)
                allowed.add(user_id)
            else:
                admins.discard(user_id)
                if not admins:
                    raise ValueError('Нельзя удалить последнего администратора.')
                if role == 'user':
                    allowed.add(user_id)
                else:
                    allowed.discard(user_id)
                    config['profile_owners'] = {key: [item for item in values if item != user_id]
                                               for key, values in (config.get('profile_owners') or {}).items()}
            config.update(admin_chat_ids=sorted(admins), allowed_chat_ids=sorted(allowed | admins))
            atomic_write_text(self.path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')

    def bind_existing_profile(self, user_id: int, profile_key: str, username: str, display_name: str = '') -> None:
        with file_lock(self.path):
            config = self._read()
            allowed = parse_chat_ids(config.get('allowed_chat_ids', []))
            if user_id not in allowed:
                raise PermissionError('Пользователю не разрешён доступ.')
            removed = self.adapter.bind_telegram_profile(profile_key, user_id, username, display_name)
            owners = {key: ids for key, ids in (config.get('profile_owners') or {}).items() if key not in removed}
            owners[profile_key] = sorted(set(parse_chat_ids(owners.get(profile_key, []))) | {user_id})
            config.update(profile_owners=owners)
            config.setdefault('admin_chat_ids', allowed)
            atomic_write_text(self.path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')

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

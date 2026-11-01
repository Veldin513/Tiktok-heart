"""Compatible per-profile settings on top of the existing profiles file."""
from __future__ import annotations

import hashlib
import json


def profile_record(payload, key: str, *, update: bool = False) -> dict:
    if isinstance(payload, dict) and key in payload:
        raw = payload[key]
        if isinstance(raw, list):
            raw = {'targets': raw}
            if update:
                payload[key] = raw
        if isinstance(raw, dict):
            return raw
    if isinstance(payload, list):
        for index, raw in enumerate(payload):
            if isinstance(raw, dict) and str(raw.get('key') or raw.get('id') or raw.get('name') or f'profile_{index}') == key:
                return raw
    raise ValueError('Профиль не найден. Обновите список профилей.')


def message_lines(text: str) -> list[str]:
    if len(text) > 500_000:
        raise ValueError('Слишком много текста. Оставьте до 1000 сообщений.')
    values = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')))
    if not values or len(values) > 1000 or any(len(value) > 3000 for value in values):
        raise ValueError('Введите от 1 до 1000 сообщений, каждое с новой строки и не длиннее 3000 символов.')
    return values


def effective_messages(record: dict, shared: list[str]) -> tuple[str, list[str]]:
    settings = record.get('message_settings')
    if settings is None or isinstance(settings, dict) and settings.get('source', 'shared') == 'shared':
        return 'shared', shared
    if not isinstance(settings, dict) or settings.get('source') != 'personal':
        raise ValueError('Не удалось прочитать настройки сообщений профиля. Данные сохранены.')
    values = settings.get('messages')
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError('Личные сообщения профиля повреждены. Данные сохранены.')
    return 'personal', message_lines('\n'.join(values))


def messages_revision(source: str, values: list[str]) -> str:
    return hashlib.sha256(json.dumps([source, values], ensure_ascii=False).encode('utf-8')).hexdigest()

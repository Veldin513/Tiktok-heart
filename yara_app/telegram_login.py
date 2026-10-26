"""Plain-language Telegram login prompts with profile-scoped access."""
from __future__ import annotations

import logging

from yara_app.interactive_login import InteractiveLogin
from yara_app.telegram_profiles import keyboard, profile_token
from yara_app.telegram_setup import TelegramSetup, parse_chat_ids

logger = logging.getLogger(__name__)


class TelegramLoginControls:
    def _init_login(self):
        self.login = InteractiveLogin(self.adapter, self._login_event, self._login_allowed)

    def _login_allowed(self, owner: int, profile: str) -> bool:
        config = TelegramSetup(self.adapter)._read()
        allowed = parse_chat_ids(config.get('allowed_chat_ids', []))
        admins = parse_chat_ids(config.get('admin_chat_ids', allowed))
        owners = (config.get('profile_owners') or {}).get(profile, [])
        return (owner in set(allowed) | set(admins) and (owner in admins or owner in owners)
                and any(p.key == profile for p in self.adapter.get_profiles()))

    def _login_event(self, session, text: str, photo=None):
        if session.stage in {'done', 'failed', 'cancelled'}:
            rows = [[('К профилю', f'p:{profile_token(session.profile)}:open')]]
            if session.stage == 'failed':
                rows.insert(0, [('Попробовать снова', f'p:{profile_token(session.profile)}:login_info')])
                rows.insert(1, [('Войти на компьютере', f'p:{profile_token(session.profile)}:login')])
        else:
            rows = [[('Отмена', f'auth:{session.id}:cancel')]]
            if session.stage == 'qr':
                rows.insert(0, [('Проверить вход', f'auth:{session.id}:check'), ('Новый QR', f'auth:{session.id}:refresh')])
            elif session.stage in {'code', 'code_request'}:
                rows.insert(0, [('Получить код', f'auth:{session.id}:request_code')])
        if photo:
            result = TelegramSetup(self.adapter)._call('sendPhoto', {
                'chat_id': session.owner, 'caption': text, 'reply_markup': keyboard(rows)}, photo_path=photo)
            if not result.get('message_id'):
                raise RuntimeError('QR photo was not acknowledged')
        else:
            self.api.send_message(session.owner, text, keyboard(rows))

    def _handle_login_input(self, message: dict) -> bool:
        chat_id = int(message['chat']['id'])
        if self.login.current(chat_id) is None:
            return False
        # Delete input before validating it: even a mistaken password/code must
        # not stay in chat merely because it had the wrong format.
        try:
            self.api.call('deleteMessage', {'chat_id': chat_id, 'message_id': message['message_id']})
        except Exception:
            self.api.send_message(chat_id, 'Не получилось удалить ваше сообщение. Удалите его из чата вручную.')
        return self.login.submit(chat_id, str(message.get('text') or ''))

    def _handle_login_callback(self, chat_id: int, data: str) -> None:
        parts = data.split(':')
        if len(parts) != 3:
            raise ValueError('Кнопка устарела. Начните вход заново.')
        self.login.command(chat_id, parts[1], parts[2])
        if parts[2] == 'check':
            self.api.send_message(chat_id, 'Если QR отсканирован, подтвердите вход в TikTok на телефоне. Я сообщу, когда всё готово.')

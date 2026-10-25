"""Profile-scoped Telegram controls. Every callback rechecks ownership."""
from __future__ import annotations

import hashlib
import re
import time

from yara_app.telegram_setup import TelegramSetup


def keyboard(rows):
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row] for row in rows]}


def profile_token(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class PersonalProfileControls:
    def _is_admin(self, chat_id: int) -> bool:
        admins = getattr(self.settings, 'admin_chat_ids', None)
        return chat_id in (self.settings.allowed_chat_ids if admins is None else admins)

    def _can_access_profile(self, chat_id: int, key: str) -> bool:
        return self._is_admin(chat_id) or chat_id in getattr(self.settings, 'profile_owners', {}).get(key, [])

    def _user_profiles(self, chat_id: int):
        return [profile for profile in self.adapter.get_profiles() if self._can_access_profile(chat_id, profile.key)]

    def _ensure_user_profile(self, message: dict) -> None:
        chat_id = int(message['chat']['id'])
        user = message.get('from') or message['chat']
        TelegramSetup(self.adapter).ensure_user_profile(chat_id, str(user.get('username') or ''),
                                                      str(user.get('first_name') or ''))
        self.settings = self._load_settings()

    def _send_id(self, chat_id: int) -> None:
        role = 'администратор' if self._is_admin(chat_id) else ('пользователь' if self._authorized(chat_id) else 'доступ не выдан')
        self.api.send_message(chat_id, f'🆔 Ваш Telegram ID: {chat_id}\nПрава: {role}.\n'
                              'Для доступа передайте этот ID администратору приложения.')

    def _view_status(self, chat_id: int) -> str:
        if self._is_admin(chat_id):
            return self.adapter.render_status_text()
        profiles = self._user_profiles(chat_id)
        if not profiles:
            return 'Ваш личный профиль ещё не создан. Отправьте /start.'
        lines = []
        for profile in profiles:
            status = self.adapter.get_profile_worker_status(profile.key)
            queue = self.adapter.queue_snapshot(profile.key)
            lines.append(f'{profile.label}\nWorker: {"работает" if status.running else "остановлен"}\n'
                         f'Адресатов: {queue.get("target_count", 0)}; готовы: {queue.get("ready_count", 0)}')
        return '\n\n'.join(lines)

    def _profile_by_token(self, chat_id: int, token: str):
        profile = next((item for item in self.adapter.get_profiles() if profile_token(item.key) == token), None)
        if profile is None or not self._can_access_profile(chat_id, profile.key):
            raise PermissionError('Этот профиль вам недоступен. Обновите меню.')
        return profile

    def _send_profile_list(self, chat_id: int, message_id=None) -> None:
        profiles = self._user_profiles(chat_id)
        rows = [[(profile.label[:55], f'p:{profile_token(profile.key)}:open')] for profile in profiles[:40]]
        rows.append([('🆔 Мой ID', 'nav:myid'), ('⬅ Главное меню', 'nav:main')])
        text = 'Выберите профиль:' if profiles else 'Личный профиль ещё не создан. Отправьте /start.'
        self._safe_edit_or_send(chat_id, text, keyboard(rows), message_id)

    def _send_profile_panel(self, chat_id: int, profile, message_id=None) -> None:
        token = profile_token(profile.key)
        targets = profile.raw.get('targets') if isinstance(profile.raw, dict) else None
        targets = targets or []
        lines = [profile.label, f'Профиль: {profile.key}']
        status = self.adapter.get_profile_worker_status(profile.key)
        lines.append('Worker работает' if status.running else 'Worker остановлен')
        rows = []
        for index, target in enumerate(targets[:20]):
            state = self.adapter.get_target_state(target['name'], profile.key)
            lines.append(f'Адресат: {target.get("url") or target["name"]}; серия: {state["streak_count"]}')
            rows.append([(f'👤 Изменить адресата {index + 1}', f'p:{token}:target:{index}'),
                         (f'🔥 Серия {index + 1}', f'p:{token}:streak:{index}')])
        if not targets:
            lines.append('Адресат пока не настроен.')
            rows.append([('👤 Настроить адресата', f'p:{token}:target:0')])
        rows += [
            [('🔐 Войти в TikTok', f'p:{token}:login'), ('✅ Проверить вход', f'p:{token}:verify')],
            [('Отменить незавершённый вход', f'p:{token}:cancel_login')],
            [('▶ Запустить мой worker', f'p:{token}:start'), ('⏹ Остановить мой worker', f'p:{token}:stop')],
            [('♻ Обновить профиль', f'p:{token}:open'), ('⬅ Профили', 'nav:recipients')],
        ]
        self._safe_edit_or_send(chat_id, '\n'.join(lines), keyboard(rows), message_id)

    def _handle_profile_callback(self, chat_id: int, message_id: int, data: str) -> None:
        parts = data.split(':')
        if len(parts) < 3:
            raise ValueError('Кнопка устарела. Отправьте /start.')
        profile = self._profile_by_token(chat_id, parts[1])
        action = parts[2]
        if action == 'open':
            self._send_profile_panel(chat_id, profile, message_id)
            return
        if action in {'target', 'streak'}:
            index = int(parts[3])
            targets = profile.raw.get('targets') or []
            if index < 0 or index >= max(1, len(targets)) or (action == 'streak' and not targets):
                raise ValueError('Адресат не найден. Обновите профиль.')
            target = targets[index] if targets else None
            self.pending_profile_inputs[chat_id] = {'profile': profile.key, 'action': action, 'index': index,
                'name': target['name'] if target else None, 'url': target.get('url') if target else None, 'created_at': time.time()}
            prompt = ('Пришлите @username или ссылку нового адресата TikTok. Для нового адресата серия и пауза начнутся заново.'
                      if action == 'target' else 'Пришлите число дней серии от 0 до 1000000.')
            self.api.send_message(chat_id, prompt + '\nОтмена: /cancel')
            return
        if action == 'start':
            result = self.adapter.start_profile_worker(profile.key)
            text = 'Worker этого профиля запущен.' if result.running else 'Worker не запущен.'
        elif action == 'stop':
            self.adapter.stop_profile_worker(profile.key)
            text = 'Worker этого профиля остановлен.'
        elif action == 'login':
            self.adapter.begin_dedicated_login(profile.key)
            text = 'На компьютере открыто отдельное окно Chrome для этого профиля. Войдите в TikTok, закройте окно и нажмите «Проверить вход».'
        elif action == 'verify':
            result = self.adapter.finish_dedicated_login(profile.key)
            text = str(result.get('message') or 'Проверка входа завершена.')
        elif action == 'cancel_login':
            self.adapter.cancel_dedicated_login(profile.key)
            text = 'Незавершённый вход отменён.'
        else:
            raise ValueError('Неизвестная кнопка. Обновите профиль.')
        self.api.send_message(chat_id, text)
        self._send_profile_panel(chat_id, profile)

    def _handle_profile_input(self, chat_id: int, text: str) -> bool:
        pending = self.pending_profile_inputs.get(chat_id)
        if not pending:
            return False
        if time.time() - pending['created_at'] > 600:
            self.pending_profile_inputs.pop(chat_id, None)
            self.api.send_message(chat_id, 'Время ожидания истекло. Выберите кнопку ещё раз.')
            return True
        profile = next((item for item in self._user_profiles(chat_id) if item.key == pending['profile']), None)
        if profile is None:
            self.pending_profile_inputs.pop(chat_id, None)
            raise PermissionError('Доступ к этому профилю отозван.')
        if self.adapter.get_profile_worker_status(profile.key).running:
            raise ValueError('Сначала остановите worker этого профиля.')
        if pending['action'] == 'streak':
            if not re.fullmatch(r'\d{1,7}', text) or int(text) > 1000000:
                raise ValueError('Пришлите целое число от 0 до 1000000 или /cancel.')
            targets = profile.raw.get('targets') or []
            index = pending['index']
            if index >= len(targets) or targets[index]['name'] != pending['name'] or targets[index].get('url') != pending['url']:
                raise ValueError('Адресат уже изменён. Выберите его заново.')
            self.adapter.set_target_streak(pending['name'], int(text), profile.key)
            message = 'Серия сохранена.'
        else:
            if pending['name'] is None:
                normalized = text.strip().rstrip('/').split('@')[-1]
                self.adapter.add_target(profile.key, normalized, text)
            else:
                self.adapter.update_target_recipient(profile.key, pending['index'], text,
                    expected_name=pending['name'], expected_url=pending['url'])
            message = 'Адресат сохранён.'
        self.pending_profile_inputs.pop(chat_id, None)
        self.api.send_message(chat_id, message)
        self._send_profile_panel(chat_id, next(item for item in self.adapter.get_profiles() if item.key == profile.key))
        return True

    def _send_access(self, chat_id: int) -> None:
        if not self._is_admin(chat_id):
            raise PermissionError('Только для администратора.')
        admins = sorted(self.settings.admin_chat_ids or [])
        self.api.send_message(chat_id, 'Администраторы: ' + ', '.join(map(str, admins)) + '\n'
            'Разрешить личный профиль пользователю: /user ID\n'
            'Назначить администратора: /admin ID\nОтозвать доступ: /revoke ID\n'
            'Пользователь узнаёт свой ID кнопкой «Мой ID» или командой /id.')

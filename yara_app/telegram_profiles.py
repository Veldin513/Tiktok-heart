"""Profile-scoped Telegram controls. Every callback rechecks ownership."""
from __future__ import annotations

import hashlib
import re
import secrets
import time

from yara_app.native_login import LOGIN_PENDING
from yara_app.telegram_setup import TelegramSetup


def keyboard(rows):
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row] for row in rows]}


def profile_token(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class PersonalProfileControls:
    def _clear_pending(self, chat_id: int) -> None:
        self.pending_profile_inputs.pop(chat_id, None)
        self.pending_access_inputs.pop(chat_id, None)
        self.access_confirmations.pop(chat_id, None)

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
        text = f'🆔 Ваш Telegram ID: {chat_id}\nПрава: {role}.'
        if not self._authorized(chat_id):
            text += '\nПередайте этот ID администратору приложения, чтобы получить доступ.'
        self.api.send_message(chat_id, text, keyboard([[('🏠 Главное меню', 'nav:main')]]))

    def _view_status(self, chat_id: int) -> str:
        if self._is_admin(chat_id):
            state = self.adapter.get_control_state()
            running = self.adapter.get_worker_status().running
            profiles = self.adapter.get_profiles()
            active = next((p.label for p in profiles if p.key == state.get('active_profile')), 'не выбран')
            return ('Общее состояние\n'
                    f'Отправка: {"выполняется" if running else "не запущена"}\n'
                    f'Профиль общего расписания: {active}\n'
                    f'Общая пауза: {"включена" if state.get("paused") else "выключена"}\n'
                    f'Профилей: {len(profiles)}\n\n'
                    'Для адресата, серии и входа откройте нужный профиль.')
        profiles = self._user_profiles(chat_id)
        if not profiles:
            return 'Ваш личный профиль ещё не создан. Отправьте /start.'
        lines = []
        for profile in profiles:
            status = self.adapter.get_profile_worker_status(profile.key)
            queue = self.adapter.queue_snapshot(profile.key)
            lines.append(f'{profile.label}\nОтправка: {"выполняется" if status.running else "не запущена"}\n'
                         f'Адресатов: {queue.get("target_count", 0)}; готовы: {queue.get("ready_count", 0)}')
        return '\n\n'.join(lines)

    def _profile_by_token(self, chat_id: int, token: str):
        profile = next((item for item in self.adapter.get_profiles() if profile_token(item.key) == token), None)
        if profile is None or not self._can_access_profile(chat_id, profile.key):
            raise PermissionError('Этот профиль вам недоступен. Обновите меню.')
        return profile

    def _send_profile_list(self, chat_id: int, message_id=None, page: int = 0) -> None:
        self._clear_pending(chat_id)
        profiles = self._user_profiles(chat_id)
        if len(profiles) == 1 and not self._is_admin(chat_id):
            self._send_profile_panel(chat_id, profiles[0], message_id)
            return
        pages = max(1, (len(profiles) + 5) // 6)
        page = min(max(0, page), pages - 1)
        rows = [[(profile.label[:55], f'p:{profile_token(profile.key)}:open')] for profile in profiles[page * 6:page * 6 + 6]]
        pager = []
        if page:
            pager.append(('⬅ Назад', f'nav:recipients:{page - 1}'))
        if page + 1 < pages:
            pager.append(('Далее ➡', f'nav:recipients:{page + 1}'))
        if pager:
            rows.append(pager)
        rows.append([('🆔 Мой ID', 'nav:myid'), ('⬅ Главное меню', 'nav:main')])
        text = f'Выберите профиль · {page + 1}/{pages}:\nЗдесь настраиваются адресат, вход и отправка.' if profiles else 'Личный профиль ещё не создан. Отправьте /start.'
        self._safe_edit_or_send(chat_id, text, keyboard(rows), message_id)

    def _send_profile_panel(self, chat_id: int, profile, message_id=None, notice: str = '', page: int = 0) -> None:
        token = profile_token(profile.key)
        targets = profile.raw.get('targets') if isinstance(profile.raw, dict) else None
        targets = targets or []
        lines = [notice, profile.label] if notice else [profile.label]
        status = self.adapter.get_profile_worker_status(profile.key)
        lines.append('▶ Отправка выполняется' if status.running else '⏹ Отправка не запущена')
        pending_login = (self.adapter._bot_profile_root(profile.key) / 'browser' / LOGIN_PENDING).exists()
        confirmed = self.adapter.profile_login_confirmed(profile.key)
        lines.append('🔐 Вход не завершён: закройте окно Chrome и нажмите «Я вошёл — проверить».' if pending_login else
                     ('✅ Вход был подтверждён при последней проверке.' if confirmed else '🔐 Сначала войдите в TikTok.'))
        if not profile.enabled:
            lines.append('⏸ Профиль отключён администратором.')
        rows = []
        pages = max(1, (len(targets) + 7) // 8)
        page = min(max(0, page), pages - 1)
        for index, target in enumerate(targets[page * 8:page * 8 + 8], start=page * 8):
            state = self.adapter.get_target_state(target['name'], profile.key)
            lines.append(f'Адресат: {target.get("url") or target["name"]}; серия: {state["streak_count"]}')
            rows.append([(f'👤 Изменить адресата {index + 1}', f'p:{token}:target:{index}'),
                         (f'🔥 Серия {index + 1}', f'p:{token}:streak:{index}')])
        pager = []
        if page:
            pager.append(('⬅ Адресаты', f'p:{token}:open:{page - 1}'))
        if page + 1 < pages:
            pager.append(('Адресаты ➡', f'p:{token}:open:{page + 1}'))
        if pager:
            rows.append(pager)
        if not targets:
            lines.append('Адресат пока не настроен.')
            rows.append([('👤 Настроить адресата', f'p:{token}:target:0')])
        if pending_login:
            rows += [[('✅ Я вошёл — проверить', f'p:{token}:verify')],
                     [('↩ Отменить вход', f'p:{token}:cancel_login')]]
        elif not confirmed:
            rows.append([('🔐 Войти в TikTok', f'p:{token}:login_info')])
        else:
            rows.append([('🔐 Настройки входа', f'p:{token}:login_info')])
        rows += [
            [('⏹ Остановить отправку', f'p:{token}:stop')] if status.running else
            [('▶ Запустить отправку', f'p:{token}:start')],
            [('♻ Обновить профиль', f'p:{token}:open'), ('⬅ Профили', 'nav:recipients')],
        ]
        if self._is_admin(chat_id):
            rows.insert(-1, [('⏸ Отключить профиль' if profile.enabled else '▶ Включить профиль', f'p:{token}:enabled'),
                             ('📅 Для расписания', f'p:{token}:activate')])
        lines.append('Запуск из Telegram не ждёт окончания кулдауна. Таймер обновится только после подтверждённой отправки.')
        self._safe_edit_or_send(chat_id, '\n'.join(lines), keyboard(rows), message_id)

    def _handle_profile_callback(self, chat_id: int, message_id: int, data: str) -> None:
        parts = data.split(':')
        if len(parts) < 3:
            raise ValueError('Кнопка устарела. Отправьте /start.')
        profile = self._profile_by_token(chat_id, parts[1])
        action = parts[2]
        self._clear_pending(chat_id)
        if action == 'open':
            self._send_profile_panel(chat_id, profile, message_id, page=int(parts[3]) if len(parts) == 4 else 0)
            return
        if action in {'target', 'streak'}:
            if len(parts) != 4 or not parts[3].isdigit():
                raise ValueError('Кнопка устарела. Откройте профиль заново.')
            index = int(parts[3])
            targets = profile.raw.get('targets') or []
            if index < 0 or index >= max(1, len(targets)) or (action == 'streak' and not targets):
                raise ValueError('Адресат не найден. Обновите профиль.')
            target = targets[index] if targets else None
            self.pending_profile_inputs[chat_id] = {'profile': profile.key, 'action': action, 'index': index,
                'name': target['name'] if target else None, 'url': target.get('url') if target else None, 'created_at': time.time()}
            prompt = ('Напишите ник адресата, например @username, или ссылку https://www.tiktok.com/@username.\n'
                      'При смене человека его серия начнётся с нуля; прежняя история сохранится отдельно.'
                      if action == 'target' else 'Сколько дней в серии? Напишите число, например 317. Допустимо от 0 до 1000000.')
            self._safe_edit_or_send(chat_id, profile.label + '\n\n' + prompt,
                                    keyboard([[('↩ Отмена', 'input:cancel')]]), message_id)
            return
        if action == 'login_info':
            self._safe_edit_or_send(chat_id, profile.label + '\n\nВход в TikTok\n'
                'Окно Chrome откроется на компьютере, где установлено приложение. '
                'Войдите там в аккаунт этого профиля, закройте окно и нажмите «Я вошёл — проверить».\n'
                'Если вход уже работает, повторять его не нужно.', keyboard([
                    [('🔐 Открыть Chrome на компьютере', f'p:{parts[1]}:login')],
                    [('⬅ К профилю', f'p:{parts[1]}:open')]]), message_id)
            return
        if action == 'start':
            result = self.adapter.start_profile_worker(profile.key, ignore_cooldown=True)
            text = '▶ Отправка запущена. Итог придёт сюда после завершения.' if result.running else '⚠ Отправка не запущена. Проверьте журнал в приложении.'
        elif action == 'stop':
            self.adapter.stop_profile_worker(profile.key)
            text = '⏹ Отправка этого профиля остановлена.'
        elif action == 'login':
            self.adapter.begin_dedicated_login(profile.key)
            text = 'На компьютере открыто отдельное окно Chrome для этого профиля. Войдите в TikTok, закройте окно и нажмите «Проверить вход».'
        elif action == 'verify':
            result = self.adapter.finish_dedicated_login(profile.key)
            text = str(result.get('message') or ('✅ Вход подтверждён. Можно запускать отправку.' if result.get('verified') else
                                               'Проверка завершена. Обновите профиль, чтобы увидеть состояние входа.'))
        elif action == 'cancel_login':
            self.adapter.cancel_dedicated_login(profile.key)
            text = 'Незавершённый вход отменён.'
        elif action in {'enabled', 'activate'}:
            if not self._is_admin(chat_id):
                raise PermissionError('Эта настройка доступна только администратору.')
            if action == 'enabled':
                profile = self.adapter.toggle_profile_key(profile.key)
                text = 'Профиль включён.' if profile.enabled else 'Профиль отключён.'
            else:
                self.adapter.set_active_profile_key(profile.key)
                text = '📅 Этот профиль выбран для общего расписания на компьютере.'
        else:
            raise ValueError('Неизвестная кнопка. Обновите профиль.')
        profile = next(item for item in self.adapter.get_profiles() if item.key == profile.key)
        self._send_profile_panel(chat_id, profile, message_id, notice=text)

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
        self._send_profile_panel(chat_id, next(item for item in self.adapter.get_profiles() if item.key == profile.key), notice='✅ ' + message)
        return True

    def _send_access(self, chat_id: int, message_id=None, page: int = 0) -> None:
        if not self._is_admin(chat_id):
            raise PermissionError('Только для администратора.')
        self._clear_pending(chat_id)
        ids = sorted(self.settings.allowed_chat_ids)
        pages = max(1, (len(ids) + 5) // 6)
        page = min(max(0, page), pages - 1)
        rows = [[('➕ Добавить пользователя', 'access:add')]]
        for user_id in ids[page * 6:page * 6 + 6]:
            role = 'Администратор' if self._is_admin(user_id) else 'Пользователь'
            rows.append([(f'{role} · {user_id}', f'access:user:{user_id}')])
        pager = []
        if page:
            pager.append(('⬅ Назад', f'access:page:{page - 1}'))
        if page + 1 < pages:
            pager.append(('Далее ➡', f'access:page:{page + 1}'))
        if pager:
            rows.append(pager)
        rows.append([('🏠 Главное меню', 'nav:main')])
        self._safe_edit_or_send(chat_id, 'Пользователи и права\n\n'
            'Пользователь управляет своим профилем. Администратор — всеми профилями и доступом.\n'
            'Для добавления узнайте ID человека кнопкой «Мой ID».\n'
            f'Страница {page + 1}/{pages}. Выберите человека или добавьте нового.', keyboard(rows), message_id)

    def _handle_access_callback(self, chat_id: int, message_id: int, data: str) -> None:
        if not self._is_admin(chat_id):
            raise PermissionError('Права пользователей меняет только администратор.')
        parts = data.split(':')
        action = parts[1]
        if action == 'confirm':
            pending = self.access_confirmations.pop(chat_id, None)
            if not pending or len(parts) != 3 or parts[2] != pending['nonce'] or time.time() - pending['created_at'] > 600:
                raise ValueError('Подтверждение устарело. Выберите пользователя заново.')
            TelegramSetup(self.adapter).change_access(chat_id, pending['user_id'], pending['role'])
            self.settings = self._load_settings()
            self._send_access(chat_id, message_id)
            return
        self._clear_pending(chat_id)
        if action == 'page' and len(parts) == 3 and parts[2].isdigit():
            self._send_access(chat_id, message_id, int(parts[2]))
        elif action == 'add':
            self.pending_access_inputs[chat_id] = {'created_at': time.time()}
            self._safe_edit_or_send(chat_id, 'Напишите Telegram ID человека — только цифры.\n'
                'Он может узнать его в этом боте кнопкой «Мой ID».\n'
                'После подтверждения он получит доступ к своему профилю.',
                keyboard([[('↩ Отмена', 'input:cancel')]]), message_id)
        elif action == 'user' and len(parts) == 3 and parts[2].isdigit():
            user_id = int(parts[2])
            if user_id not in self.settings.allowed_chat_ids:
                raise ValueError('Доступ этого пользователя уже отозван.')
            profiles = self._user_profiles(user_id) if not self._is_admin(user_id) else [
                p for p in self.adapter.get_profiles() if user_id in self.settings.profile_owners.get(p.key, [])]
            rows = [[('👤 Оставить только личный доступ' if self._is_admin(user_id) else '👑 Сделать администратором',
                      f'access:role:{user_id}:{"user" if self._is_admin(user_id) else "admin"}')],
                    [('🚫 Отозвать доступ', f'access:role:{user_id}:revoke')],
                    [('⬅ Пользователи', 'nav:access')]]
            self._safe_edit_or_send(chat_id, f'Telegram ID: {user_id}\n'
                f'Права: {"администратор" if self._is_admin(user_id) else "личный профиль"}\n'
                + '\n'.join(p.label for p in profiles)[:1800], keyboard(rows), message_id)
        elif action == 'role' and len(parts) == 4 and parts[2].isdigit() and parts[3] in {'user', 'admin', 'revoke'}:
            if int(parts[2]) not in self.settings.allowed_chat_ids:
                raise ValueError('Пользователь больше не имеет доступа. Обновите список.')
            self._confirm_access(chat_id, int(parts[2]), parts[3], message_id)
        else:
            raise ValueError('Кнопка устарела. Откройте пользователей заново.')

    def _confirm_access(self, chat_id: int, user_id: int, role: str, message_id=None) -> None:
        nonce = secrets.token_hex(6)
        self.access_confirmations[chat_id] = {'user_id': user_id, 'role': role, 'created_at': time.time(), 'nonce': nonce}
        meaning = {'user': 'Доступ только к своему профилю.', 'admin': 'Полный доступ ко всем профилям и настройкам.',
                   'revoke': 'Доступ будет закрыт. Профиль, вход и история останутся на компьютере.'}[role]
        self._safe_edit_or_send(chat_id, f'Telegram ID: {user_id}\n{meaning}\n\nСохранить эти права?', keyboard([
            [('✅ Подтвердить', f'access:confirm:{nonce}')], [('↩ Отмена', 'nav:access')]]), message_id)

    def _handle_access_input(self, chat_id: int, text: str) -> bool:
        pending = self.pending_access_inputs.get(chat_id)
        if not pending:
            return False
        if not self._is_admin(chat_id):
            self._clear_pending(chat_id)
            raise PermissionError('Права администратора отозваны.')
        if time.time() - pending['created_at'] > 600:
            self._clear_pending(chat_id)
            raise ValueError('Время ввода истекло. Нажмите «Добавить пользователя» ещё раз.')
        if not re.fullmatch(r'[1-9]\d{0,15}', text):
            raise ValueError('Нужен Telegram ID из цифр, например 123456789. Имя с @ сюда не подходит.')
        self.pending_access_inputs.pop(chat_id, None)
        self._confirm_access(chat_id, int(text), 'user')
        return True

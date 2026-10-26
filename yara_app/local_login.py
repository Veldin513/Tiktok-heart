"""Desktop facade: exposes progress and QR, never echoes entered secrets."""
from __future__ import annotations

import threading

from yara_app.interactive_login import InteractiveLogin


class LocalLogin:
    def __init__(self, adapter):
        self.adapter = adapter
        self.lock = threading.RLock()
        self.events = {}
        self.revision = 0
        self.engine = InteractiveLogin(adapter, self._event, self._allowed, channel='desktop')

    def _allowed(self, owner, profile):
        return owner == 0 and any(p.key == profile for p in self.adapter.get_profiles())

    def _event(self, session, text, photo=None):
        text = {'identifier': 'Введите логин или почту TikTok.', 'password': 'Введите пароль.',
                'code': 'Введите код подтверждения.',
                'code_request': 'Нажмите «Получить код».',
                'qr': 'Отсканируйте QR в TikTok на телефоне и подтвердите вход.',
                'waiting': 'Подождите… Если TikTok попросит подтверждение на телефоне, подтвердите.'}.get(session.stage, text)
        with self.lock:
            self.revision += 1
            self.events[session.id] = {'id': session.id, 'profile': session.profile,
                                      'stage': session.stage, 'message': text, 'photo': photo, 'revision': self.revision}

    def start(self, profile, method):
        session = self.engine.start(profile, 0, method)
        with self.lock:
            self.events = {session.id: self.events.get(session.id, {'id': session.id, 'profile': profile,
                'stage': 'starting', 'message': 'Открываем вход…', 'photo': None})}
        return self.status(session.id)

    def status(self, sid):
        with self.lock:
            data = self.events.get(sid)
            if data is None:
                raise ValueError('Вход уже закрыт. Начните заново.')
            result = {key: value for key, value in data.items() if key != 'photo'}
            result['has_photo'] = bool(data.get('photo') and data['stage'] in {'qr', 'code'})
            result['field'] = data['stage'] if data['stage'] in {'identifier', 'password', 'code'} else None
            current = self.engine.current(0)
            if current and current.id == sid and current.stage == 'busy':
                result.update(stage='busy', field=None, message='Проверяем…')
            return result

    def image(self, sid):
        with self.lock:
            data = self.events.get(sid) or {}
            if data.get('stage') not in {'qr', 'code'} or data.get('photo') is None:
                raise ValueError('QR-код уже закрыт.')
            path = data['photo']
            folder = self.adapter._bot_profile_root(data['profile']).resolve() / 'artifacts'
            if not path.resolve().is_relative_to(folder):
                raise ValueError('Изображение недоступно.')
            with path.open('rb') as image:
                result = image.read(8 * 1024 * 1024 + 1)
            if len(result) > 8 * 1024 * 1024:
                raise ValueError('Не удалось показать QR. Попробуйте ещё раз.')
            return result

    def submit(self, sid, value):
        current = self.engine.current(0)
        if current is None or current.id != sid:
            raise ValueError('Вход уже закрыт. Начните заново.')
        self.engine.submit(0, value)
        return {'stage': 'busy', 'message': 'Проверяем…', 'id': sid, 'has_photo': False, 'field': None}

    def command(self, sid, action):
        self.engine.command(0, sid, action)
        if action == 'cancel':
            with self.lock:
                if self.events[sid]['stage'] not in {'done', 'failed', 'cancelled'}:
                    self.events[sid].update(stage='cancelling', message='Отменяем вход…', photo=None)
        return self.status(sid)

    def close(self):
        self.engine.close()

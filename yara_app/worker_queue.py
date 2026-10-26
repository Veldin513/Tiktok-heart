"""Persistent FIFO. A single dispatcher waits on process exit, never a timer."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import time
from contextlib import ExitStack, suppress

import psutil

from yara_app.fileio import atomic_write_text, file_lock, read_json
from yara_app.notifications import notify_worker_result
from yara_app.telegram_setup import parse_chat_ids

logger = logging.getLogger(__name__)


def targets_fingerprint(profile) -> str:
    return hashlib.sha256(json.dumps(profile.raw.get('targets') or [], ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


class WorkerQueue:
    def __init__(self, adapter):
        self.adapter = adapter
        self.path = adapter.control_dir / 'worker_queue.json'
        self.launch_lock = adapter.control_dir / 'worker_launch'
        self.dispatch_lock = adapter.control_dir / 'worker_queue_dispatch'

    def read(self):
        data = read_json(self.path, {'waiting': [], 'current': None, 'history': []})
        if (not isinstance(data, dict) or not isinstance(data.get('waiting'), list)
                or not isinstance(data.get('history'), list) or len(data['waiting']) > 100
                or data.get('current') is not None and not isinstance(data['current'], dict)):
            raise ValueError('Не удалось прочитать очередь. Данные сохранены; обратитесь к администратору.')
        profiles = set()
        for index, item in enumerate(data['waiting'] + ([data['current']] if data['current'] is not None else [])):
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or not isinstance(item.get('profile'), str) or not isinstance(item.get('fingerprint'), str)
                    or type(item.get('ignore_cooldown')) is not bool or type(item.get('boot_force')) is not bool
                    or type(item.get('dry_run')) is not bool
                    or item.get('requester') is not None and (type(item['requester']) is not int or item['requester'] <= 0)
                    or index < len(data['waiting']) and item['profile'] in profiles):
                raise ValueError('Очередь повреждена. Автоматический запуск остановлен, данные сохранены.')
            if index < len(data['waiting']):
                profiles.add(item['profile'])
        return data

    def save(self, data):
        data['history'] = data['history'][-50:]
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def snapshot(self):
        try:
            data = self.read()
        except ValueError:
            return {'waiting': [], 'current': None, 'paused': True,
                    'error': 'Очередь не удалось прочитать. Данные сохранены; нужен администратор.'}
        labels = {p.key: p.label for p in self.adapter.get_profiles()}
        return {'waiting': [{'profile': item['profile'], 'label': labels.get(item['profile'], item['profile']),
                             'position': index + 1, 'created_at': item.get('created_at')}
                            for index, item in enumerate(data['waiting'])],
                'current': data.get('current'), 'paused': bool(self.adapter.get_control_state().get('paused'))}

    def position(self, profile):
        return next((item['position'] for item in self.snapshot()['waiting'] if item['profile'] == profile), None)

    def authorize(self, profile, actor):
        if actor is not None:
            config = read_json(self.adapter.telegram_config_path, {})
            allowed = parse_chat_ids(config.get('allowed_chat_ids', []))
            admins = parse_chat_ids(config.get('admin_chat_ids', allowed))
            owners = parse_chat_ids((config.get('profile_owners') or {}).get(profile, []))
            if actor not in set(allowed) | set(admins) or actor not in admins and actor not in owners:
                raise PermissionError('Доступ к профилю изменился. Запуск из очереди отменён.')

    def enqueue_locked(self, profile, *, ignore_cooldown=False, requester=None):
        data = self.read()
        position = next((i + 1 for i, item in enumerate(data['waiting']) if item['profile'] == profile.key), None)
        if position is not None:
            changed = False
            if ignore_cooldown:
                data['waiting'][position - 1].update(ignore_cooldown=True, requester=requester)
                changed = True
            if str(os.getenv('WORKER_FORCE_SEND_ON_BOOT', '')).lower() in {'1', 'true', 'yes', 'on'}:
                data['waiting'][position - 1]['boot_force'] = True
                changed = True
            if changed:
                self.save(data)
            return position
        if len(data['waiting']) >= 100:
            raise ValueError('Очередь заполнена. Дождитесь завершения отправок.')
        data['waiting'].append({'id': secrets.token_hex(12), 'profile': profile.key, 'requester': requester,
            'ignore_cooldown': ignore_cooldown,
            'dry_run': bool(self.adapter.get_control_state().get('dry_run')),
            'boot_force': str(os.getenv('WORKER_FORCE_SEND_ON_BOOT', '')).lower() in {'1', 'true', 'yes', 'on'},
            'created_at': time.time(), 'fingerprint': targets_fingerprint(profile)})
        self.save(data)
        logger.info('Профиль %s добавлен в очередь: место %s', profile.key, len(data['waiting']))
        return len(data['waiting'])

    def cancel(self, profile=None):
        with file_lock(self.launch_lock):
            data = self.read()
            removed = [item for item in data['waiting'] if profile is None or item['profile'] == profile]
            data['waiting'] = [item for item in data['waiting'] if item not in removed]
            for item in removed:
                data['history'].append({**item, 'status': 'cancelled', 'finished_at': time.time()})
            self.save(data)
            logger.info('Из очереди убрано ожидающих запусков: %s', len(removed))
            return len(removed)

    def kick(self):
        data = self.read()
        if not data['waiting'] and not data.get('current'):
            return True
        try:
            with file_lock(self.dispatch_lock, timeout=0):
                pass
        except TimeoutError:
            return True
        script = self.adapter.base_dir / 'scripts' / 'worker_queue_runner.py'
        if not script.is_file():
            return False
        env = os.environ.copy()
        env.pop('WORKER_FORCE_SEND_ON_BOOT', None)
        env.update(YARA_BASE_DIR=str(self.adapter.base_dir), PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        env['PYTHONPATH'] = str(self.adapter.base_dir) + os.pathsep + env.get('PYTHONPATH', '')
        output = self.adapter.common_logs_dir / 'worker_queue.log'
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('a', encoding='utf-8') as log:
            subprocess.Popen([self.adapter._resolve_python_executable(prefer_windowless=True), '-u', str(script),
                              '--base-dir', str(self.adapter.base_dir)], cwd=str(self.adapter.base_dir),
                             env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             **self.adapter._windows_subprocess_kwargs(hide_window=True))
        return True

    def _validate(self, job):
        profile = next((p for p in self.adapter.get_profiles() if p.key == job['profile']), None)
        if profile is None or not profile.enabled:
            raise ValueError('Профиль удалён или отключён. Запуск из очереди отменён.')
        if not profile.raw.get('targets'):
            raise ValueError('В профиле нет получателя. Укажите его и запустите отправку ещё раз.')
        if bool(self.adapter.get_control_state().get('dry_run')) != job['dry_run']:
            raise ValueError('Режим проверки или отправки изменился. Нажмите запуск ещё раз для выбранного режима.')
        self.authorize(profile.key, job.get('requester'))
        if targets_fingerprint(profile) != job['fingerprint']:
            raise ValueError('Получатели изменились. Нажмите запуск ещё раз, чтобы подтвердить новый список.')
        state = self.adapter._get_profile_state_dir(profile.key)
        if (state / 'session_import_required.json').exists():
            raise ValueError('Нужно войти в TikTok. После входа нажмите запуск ещё раз.')
        return profile

    def _report_failed(self, job, message):
        notify_worker_result(self.adapter, {'profile_name': job['profile'], 'failed_count': 1,
            'results': [{'target': '__run__', 'reason': 'queue_blocked', 'error': message}]},
            status='failed', reason='queue_blocked')

    def _finish(self, job, status, error=None):
        with file_lock(self.launch_lock):
            data = self.read()
            if data.get('current') and data['current'].get('id') == job['id']:
                data['current'] = None
                data['history'].append({**job, 'status': status, 'finished_at': time.time(), 'error': error})
                self.save(data)

    @staticmethod
    def wait_for_process(pid):
        with suppress(psutil.NoSuchProcess):
            # On Windows this blocks on a process handle, without periodic
            # queue checks. PID reuse cannot redirect an already opened handle.
            psutil.Process(pid).wait()

    def run(self):
        with ExitStack() as lease:
            try:
                lease.enter_context(file_lock(self.dispatch_lock, timeout=0))
            except TimeoutError:
                return
            self._run_owned(lease)

    def _run_owned(self, lease):
        while True:
            failure = None
            interrupted = None
            job = None
            with file_lock(self.launch_lock):
                data = self.read()
                status = self.adapter.get_worker_status()
                current = data.get('current')
                if status.running:
                    pid = status.pid
                else:
                    if current:
                        interrupted = current
                        data['current'] = None
                        data['history'].append({**current, 'status': 'interrupted', 'finished_at': time.time()})
                        self.save(data)
                    state = self.adapter.get_control_state()
                    if state.get('paused') or state.get('stop_requested') or not data['waiting']:
                        pid = None
                        # Release before the launch lock: an enqueue arriving at
                        # shutdown must see that it needs a new dispatcher.
                        lease.close()
                    else:
                        job = data['waiting'].pop(0)
                        data['current'] = job
                        self.save(data)  # Claim before starting; never replay an uncertain launch.
                        try:
                            self._validate(job)
                            started = self.adapter._start_worker_profile(job['profile'],
                                ignore_cooldown=job['ignore_cooldown'], boot_force=job['boot_force'])
                            if not started.running or not started.pid:
                                raise RuntimeError('Не удалось запустить отправку из очереди.')
                            pid = started.pid
                            data['current'] = {**job, 'pid': pid, 'started_at': time.time()}
                            self.save(data)
                            logger.info('Начат запуск профиля %s из очереди', job['profile'])
                        except Exception as exc:
                            failure = str(exc)
                            pid = None
            if interrupted:
                self._report_failed(interrupted, 'Результат прерванного запуска нужно проверить. Автоматического повтора нет.')
            if failure:
                self._finish(job, 'failed', failure)
                self._report_failed(job, failure)
                continue
            if pid is None:
                return
            self.wait_for_process(pid)
            # A Windows Python launcher may have a runtime child with another
            # PID. Continue waiting until that worker, too, has really exited.
            if self.adapter.get_worker_status().running:
                continue
            if job is None:
                job = current
            if job:
                self._finish(job, 'finished')

"""Local, independent checks. Reading a report never starts or repairs anything."""
from __future__ import annotations

import json
import secrets
import sys
import time

from yara_app.fileio import atomic_write_text, file_lock, read_json, read_text
from yara_app.profile_settings import effective_messages, profile_record
from yara_app.redaction import redact_value


class DiagnosticService:
    def __init__(self, adapter):
        self.adapter = adapter

    def collect(self, profile_key=None, *, include_schedule=True, fresh_schedule=False):
        started = time.perf_counter()
        adapter = self.adapter
        checks = []

        def check(key, title, action, function, *, essential=False):
            tick = time.perf_counter()
            try:
                result = function()
            except Exception as exc:
                # Raw exceptions can contain credentials or file contents.
                result = {'status': 'unknown', 'state': 'Не удалось проверить',
                          'details': 'Данные не изменены. Откройте журнал проверки или повторите её.',
                          'error_type': type(exc).__name__}
            checks.append({'key': key, 'title': title, 'action': action, 'essential': essential,
                           'duration_ms': round((time.perf_counter() - tick) * 1000), **result})

        def result(status, state, details, blocking=False):
            return {'status': status, 'state': state, 'details': details, 'blocking': blocking}

        context = {'state': {}, 'profiles': [], 'profile': None}

        def configuration():
            state = read_json(adapter.control_state_path)
            if not isinstance(state, dict):
                return result('error', 'Не читаются настройки', 'Откройте управление. Не удаляйте файлы профилей и ключ.', True)
            context['state'] = state
            raw = read_json(adapter.profiles_path)
            active = str(state.get('active_profile') or '')
            if isinstance(raw, dict):
                profiles = adapter._profiles_from_mapping(raw, active)
            elif isinstance(raw, list):
                profiles = adapter._profiles_from_sequence(raw, active)
            else:
                return result('error', 'Не читается список профилей', 'Восстановите настройки из своей резервной копии.', True)
            context['profiles'] = profiles
            selected = str(profile_key or active)
            context['profile'] = next((p for p in profiles if p.key == selected), None)
            if context['profile'] is None:
                return result('error', 'Выберите профиль', 'Добавьте профиль или выберите существующий в списке.', True)
            return result('ok', 'Настройки прочитаны', 'Проверяется выбранный профиль. Профиль расписания не меняется.')

        check('configuration', 'Настройки', 'profiles', configuration, essential=True)
        profile = context['profile']

        def runtime():
            if sys.version_info < (3, 12):  # noqa: UP036 - diagnose unsupported launch environments
                return result('error', 'Старая версия Python', 'Для приложения нужен Python 3.12 или новее. Проверьте инструкцию установки.', True)
            missing = [m['module'] for m in adapter.dependency_report()['modules'] if m['required'] and not m['installed']]
            if missing or not adapter.main_script_path.is_file():
                return result('error', 'Не хватает компонентов',
                              'Не найдены: ' + ', '.join(missing + ([] if adapter.main_script_path.is_file() else ['файл отправки']))
                              + '. Проверьте установку по инструкции. Ничего автоматически не устанавливается.', True)
            return result('ok', 'Компоненты найдены', 'Для проверки запуска отдельного процесса нажмите «Самотест запуска».')

        check('runtime', 'Компоненты приложения', 'services', runtime, essential=True)

        def process():
            state = context['state']
            pid = state.get('last_run_pid')
            running = type(pid) is int and adapter._pid_matches_script(pid, adapter.main_script_path,
                                                                       started_at=state.get('last_run_started_at'))
            if running:
                key = str(state.get('last_run_profile') or state.get('active_profile') or '')
                label = next((p.label for p in context['profiles'] if p.key == key), key)
                return result('ok', 'Отправка выполняется', f'Сейчас работает профиль: {label}. Проверка не останавливает его.')
            return result('info', 'Отправка сейчас не выполняется', 'Ожидание расписания или ручного запуска — обычное состояние.')

        check('process', 'Рабочий процесс', 'services', process)

        def protection():
            from yara_app.profile_keys import ProfileProtection
            status = ProfileProtection(adapter.base_dir).status()
            if status.get('enabled') and not status.get('key_available'):
                return result('error', 'Нужна проверка защиты', 'Не удаляйте ключ и профили. Откройте раздел «Защита» и проверьте восстановление.', True)
            if status.get('problem_profiles'):
                return result('warning', 'Есть проблема с защитой профилей',
                              'Откройте «Защита». Проблема другого профиля не означает, что выбранный профиль недоступен.')
            return result('ok', 'Ключ доступен', 'Защита включена. Секретная фраза и содержимое ключа в отчёт не записываются.') if status.get('enabled') else result(
                'info', 'Защита не включена', 'При необходимости включите её в разделе «Защита». Это не ошибка запуска.')

        check('protection', 'Защита профилей', 'protection', protection, essential=True)
        if profile is not None:
            key = profile.key

            def recipients():
                if not profile.enabled:
                    return result('warning', 'Профиль выключен', 'Включите профиль, если хотите отправлять с него.', True)
                count = len(profile.raw.get('targets') or [])
                return result('ok', f'Адресатов: {count}', 'Будут использованы адресаты этого профиля.') if count else result(
                    'error', 'Нет адресата', 'Добавьте адресата в разделе «Профили и очередь».', True)

            check('recipients', 'Профиль и адресаты', 'profiles', recipients, essential=True)

            def messages():
                shared = read_text(adapter.message_pool_path) if adapter.message_pool_path.is_file() else ''
                source, values = effective_messages(profile_record(read_json(adapter.profiles_path), key),
                                                     list(dict.fromkeys(v.strip() for v in shared.splitlines()
                                                                        if v.strip() and not v.lstrip().startswith('#'))))
                if not values:
                    return result('error', 'Нет текста', 'Добавьте хотя бы одно сообщение для этого профиля.', True)
                return result('ok', f'Сообщений: {len(values)}',
                              'Используется личный набор.' if source == 'personal' else 'Используется общий набор. Одного сообщения достаточно.')

            check('messages', 'Текст сообщения', 'messages', messages, essential=True)

            def login():
                browser = adapter.browser_profile_live_status(key)
                if browser.get('protection_error'):
                    return result('error', 'Не читается защита профиля', 'Откройте «Защита». Сохраните прежний ключ и файлы восстановления.', True)
                if browser.get('manual_login_pending'):
                    return result('warning', 'Вход ещё не завершён', 'Завершите вход в разделе «Браузер». Другие профили могут работать.', True)
                if browser.get('needs_recovery'):
                    return result('error', 'Профиль браузера не читается', 'Откройте «Браузер» и выполните вход заново для этого профиля.', True)
                validation = browser.get('session_validation') or {}
                if browser.get('session_import_required') or validation.get('valid') is False:
                    return result('warning', 'Нужен вход в TikTok', 'Войдите для этого профиля в разделе «Браузер».', True)
                if not browser.get('cookies_exists'):
                    return result('warning', 'Вход не сохранён', 'Войдите в TikTok для этого профиля.', True)
                if int(browser.get('auth_backoff_left') or 0) > 0:
                    return result('warning', 'Пауза после ошибки входа', 'Проверьте вход в разделе «Браузер». Не повторяйте запуск много раз.', True)
                if validation.get('valid') is not True:
                    return result('unknown', 'Вход ещё не проверен', 'Данные браузера есть, но они не доказывают действующий вход. Откройте «Браузер».')
                return {**result('ok', 'Вход проверен ранее',
                                 'Это последняя сохранённая проверка. TikTok сейчас не открывался; действующую сессию она не гарантирует.'),
                        'verified_at': validation.get('checked_at')}

            check('login', 'Вход в TikTok', 'browser', login, essential=True)

            def delivery():
                pending = sum(bool(adapter.get_target_state(str(t.get('name') or t.get('url') or ''), key,
                                  cooldown_hours=float(context['state'].get('cooldown_hours') or 12)).get('delivery_unconfirmed'))
                              for t in profile.raw.get('targets') or [])
                if pending:
                    return result('warning', 'Есть неподтверждённая отправка',
                                  'Проверьте чат TikTok перед новой попыткой. Программа не должна повторять её сама.', True)
                run = adapter.get_current_run_snapshot(key)
                view = run.get('outcome') or {}
                if not run.get('state_exists') and not run.get('summary_exists'):
                    return result('info', 'Отправок пока нет', 'Это не ошибка. Диагностика не отправляет тестовое сообщение.')
                if run.get('is_active'):
                    return result('info', 'Итог попытки ещё не записан',
                                  'Состояние настоящего процесса указано в карточке «Рабочий процесс». Незавершённая запись не подтверждает отправку.')
                return {**result('info' if view.get('code') in {'failed', 'unknown', 'rejected'} else 'ok',
                                 view.get('label') or 'Результат не записан',
                                 'Результат последней попытки показан отдельно от текущих препятствий. Подробности — в журнале.'),
                        'attempt_at': run.get('completed_at') or run.get('updated_at')}

            check('delivery', 'Последняя отправка', 'logs', delivery, essential=True)

        def controls():
            state = context['state']
            if state.get('paused'):
                return result('warning', 'Включена общая пауза', 'Снимите паузу в управлении, когда захотите продолжить.', True)
            if state.get('dry_run'):
                return result('info', 'Режим проверки', 'Сообщения не отправляются. Для реальной отправки переключите режим в управлении.', True)
            return result('info', 'Работа остановлена пользователем', 'Будущие запуски по расписанию этим не отключаются.') if state.get('stop_requested') else result(
                'ok', 'Обычный режим', 'Общая пауза выключена.')

        check('controls', 'Режим работы', 'services', controls, essential=True)

        def queue():
            data = adapter.worker_queue().read()
            waiting = len(data['waiting'])
            held = len(data['awaiting_login'])
            return result('ok', f'Ожидают: {waiting}; ждут входа: {held}',
                          'Очередь читается. Ожидание входа одного профиля не мешает остальным. Запуск очереди этой проверкой не выполняется.')

        check('queue', 'Очередь', 'profiles', queue, essential=True)

        def schedule():
            data = adapter.get_worker_schedule_status_cached(max_age_seconds=0) if fresh_schedule else adapter.get_worker_schedule_status_cached()
            if data.get('error') or data.get('available') is False or data.get('state') == 'unknown':
                return result('unknown', 'Не удалось проверить', 'Windows не подтвердил состояние задач. Откройте управление расписанием.')
            if not data.get('installed'):
                return result('info', 'Автоматика не включена', 'Можно запускать вручную или включить расписание в управлении.')
            if not data.get('enabled', True):
                return result('warning', 'Задачи выключены', 'Windows не запустит отправку автоматически. Проверьте управление расписанием.')
            if data.get('next_run_stale'):
                return result('warning', 'Время запуска уже прошло', 'Проверьте расписание в управлении. Автоматическое исправление не выполняется.')
            parts = ['При входе в Windows: ' + ('включено' if data.get('at_logon') or data.get('at_startup') else 'не включено'),
                     'Повтор по времени: ' + ('включён' if data.get('every_12_hours') else 'не включён')]
            if data.get('next_run_time'):
                parts.append('Следующий запуск: ' + str(data['next_run_time']))
            scheduled = next((p.label for p in context['profiles'] if p.active), 'не выбран')
            return result('ok', 'Задачи Windows найдены', '. '.join(parts) + f'. Профиль расписания: {scheduled}. Успешный запуск этим ещё не доказан.')

        if include_schedule:
            check('schedule', 'Автозапуск и расписание', 'services', schedule)

        def telegram():
            config = read_json(adapter.telegram_config_path, {})
            if not isinstance(config, dict):
                return result('warning', 'Настройки не читаются', 'Откройте настройки Telegram. Отправка TikTok от этого не зависит.')
            from yara_app.telegram_setup import parse_chat_ids
            configured = bool(config.get('token') and config.get('token') != 'PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE'
                              and parse_chat_ids(config.get('allowed_chat_ids', [])))
            if not configured:
                return result('info', 'Не настроен', 'Telegram необязателен. Настроить его можно в управлении.')
            state = context['state']
            pid = state.get('telegram_bot_pid') or adapter._read_pid_file(adapter.telegram_lock_path)
            running = type(pid) is int and adapter._pid_matches_script(pid, adapter.telegram_script_path,
                                                                       started_at=state.get('telegram_bot_started_at'))
            return result('ok' if running else 'info', 'Управление включено' if running else 'Управление выключено',
                          'Настройки сохранены. Соединение с Telegram не проверялось. Отчёты об отправке могут работать и при выключенном управлении.')

        check('telegram', 'Telegram', 'services', telegram)

        def logs():
            errors = 0
            warnings = 0
            for path in (adapter.worker_stdout_path, adapter.telegram_log_path, adapter.launcher_log_path):
                if not path.is_file():
                    continue
                with path.open('rb') as stream:
                    size = stream.seek(0, 2)
                    stream.seek(max(0, size - 65536))
                    lines = stream.read(65536).decode('utf-8', errors='replace').splitlines()[-100:]
                errors += sum('[ERROR]' in line or 'Traceback (' in line for line in lines)
                warnings += sum('[WARNING]' in line for line in lines)
            return result('info', f'Записей об ошибках: {errors}; предупреждений: {warnings}',
                          'Просмотрены последние строки общих журналов. Старые ошибки сами по себе не означают текущий сбой. Тексты с данными входа в отчёт не копируются.')

        check('logs', 'Записи о сбоях', 'logs', logs)
        blockers = sum(bool(c.get('blocking')) for c in checks)
        unknown = sum(c['status'] == 'unknown' for c in checks)
        attention = sum(c['status'] in {'error', 'warning'} for c in checks)
        return redact_value({'kind': 'preview', 'checked_at': time.time(), 'duration_ms': round((time.perf_counter() - started) * 1000),
            'profile_key': profile.key if profile else str(profile_key or ''), 'profile_label': profile.label if profile else '',
            'profiles': [{'key': p.key, 'label': p.label, 'active': p.active} for p in context['profiles']],
            'checks': checks, 'blockers': blockers, 'unknown': unknown,
            'status': 'warning' if blockers or unknown or attention else 'ok',
            'summary': f'Требуют действия: {blockers}. Не удалось подтвердить: {unknown}.' if blockers or unknown else
                       'Есть замечания. Посмотрите результаты ниже.' if attention else
                       'Текущих препятствий в проверенных данных не найдено.',
            'scope': 'Локальная проверка. Без входа в TikTok, отправок и изменения настроек.'})

    def run(self, profile_key=None, *, self_test=False):
        started = time.perf_counter()
        report = self.collect(profile_key, fresh_schedule=True)
        report.update(kind='self_test' if self_test else 'check', report_id=secrets.token_hex(8))
        if self_test:
            tick = time.perf_counter()
            try:
                probe = self.adapter.worker_starter_self_test(no_browser=True, diagnostic_only=True,
                                                              profile_key=report['profile_key'] or None)
                report['self_test'] = {'ok': bool(probe.get('ok')), 'exit_code': probe.get('exit_code'),
                                       'checked_at': time.time(), 'duration_ms': round((time.perf_counter() - tick) * 1000),
                                       'details': 'Отдельный процесс завершил безопасную проверку.' if probe.get('ok') else
                                                  'Проверка отдельного процесса не пройдена. Проверьте компоненты и настройки.',
                                       'checks': (probe.get('report') or {}).get('checks', [])}
            except Exception as exc:
                report['self_test'] = {'ok': False, 'checked_at': time.time(), 'error_type': type(exc).__name__,
                                       'details': 'Не удалось выполнить проверку отдельного процесса.'}
            if not report['self_test']['ok']:
                report.update(status='warning', summary='Самотест запуска требует внимания. Посмотрите результаты ниже.')
        report['duration_ms'] = round((time.perf_counter() - started) * 1000)
        report = redact_value(report)
        try:
            path = self.adapter.common_logs_dir / 'diagnostics_history.json'
            with file_lock(path):
                history = read_json(path, [])
                if not isinstance(history, list) or any(not isinstance(item, dict) or not isinstance(item.get('checks'), list) for item in history):
                    raise ValueError('Invalid diagnostic history')
                report['log_saved'] = True
                atomic_write_text(path, json.dumps((history + [report])[-20:], ensure_ascii=False, indent=2))
        except Exception as exc:
            report.update(log_saved=False, log_error=type(exc).__name__)
        return report

    def history(self):
        data = read_json(self.adapter.common_logs_dir / 'diagnostics_history.json', [])
        if not isinstance(data, list) or any(not isinstance(item, dict) or not isinstance(item.get('checks'), list) for item in data):
            raise ValueError('Не удалось прочитать журнал проверок. Файл сохранён.')
        return redact_value(data[-20:][::-1])

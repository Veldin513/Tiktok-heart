"""One-shot worker reports; never starts/stops the manual Telegram controller."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from yara_app.redaction import redact_secrets
from yara_app.telegram_setup import TelegramSetup, parse_chat_ids

logger = logging.getLogger(__name__)

REASONS = {
    'queue_blocked': 'Запуск из очереди отменён. Причина указана ниже.',
    'session_import_required': 'Нужно снова войти в TikTok в разделе «Браузер».',
    'auth_failed': 'Не удалось подтвердить вход в TikTok.',
    'auth_check_inconclusive': 'TikTok не удалось проверить. Сообщения не отправлялись.',
    'offline': 'Нет соединения с интернетом.',
    'delivery_unconfirmed': 'TikTok не подтвердил доставку. Проверьте чат; повтор заблокирован.',
    'chat_not_confirmed': 'Не удалось подтвердить чат адресата.',
    'chat_blocked': 'Открытию чата мешает окно или ограничение TikTok.',
    'send_failed': 'Во время отправки произошла ошибка. Проверьте чат перед повтором.',
    'send_rejected': 'TikTok отклонил отправку.',
    'send_not_attempted': 'Отправка не выполнялась: чат недоступен.',
    'local_state_write_failed': 'Сообщение принято TikTok, но пауза не записалась. Повтор заблокирован.',
    'streak_write_failed': 'Сообщение принято TikTok, но счётчик серии не записался.',
    'paused': 'В приложении включена пауза.',
    'stop_requested': 'Запуск остановлен пользователем.',
    'manual_login_pending': 'Не завершён ручной вход. Закройте Chrome и проверьте вход в приложении.',
    'session_import_running': 'Профиль занят переносом входа.',
    'auth_backoff': 'Проверка входа отложена после предыдущей ошибки.',
    'no_targets': 'В профиле нет адресатов.',
    'unhandled_exception': 'Произошла ошибка запуска. Подробности в журнале приложения.',
    'worker_start_failed': 'Не удалось запустить рабочий процесс. Подробности в журнале.',
    'target_error': 'Ошибка обработки адресата. Подробности в журнале.',
    'dry_run': 'Проверочный запуск: отправка отключена.',
    'cooldown': 'Ещё не прошла пауза до следующей отправки.',
}


def _telegram_text(text: str, limit: int) -> str:
    # Telegram counts UTF-16 units; emoji can occupy two units.
    return redact_secrets(text).encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def report_text(summary: dict, *, status: str, reason: str = '', dry_run: bool = False) -> str:
    results = summary.get('results') or []
    confirmed = sum(bool(item.get('success')) and item.get('confirmation') == 'server_ack' for item in results)
    unknown = any(item.get('reason') in {'delivery_unconfirmed', 'send_failed'} for item in results)
    reported = int(summary.get('success_count') or 0)
    failed = int(summary.get('failed_count') or 0)
    if dry_run:
        title = '🧪 Проверочный запуск — отправка отключена'
    elif unknown or reason == 'delivery_unconfirmed' or reported > confirmed:
        title = '⚠️ Результат отправки требует проверки'
    elif status == 'failed' or failed:
        title = '⚠️ Отправлено не всё' if confirmed else '❌ Отправка не выполнена'
    elif confirmed:
        title = '✅ TikTok подтвердил отправку'
    else:
        title = '⏸ Отправка пропущена'
    lines = [title, f"Профиль: {summary.get('profile_name') or 'default'}",
             f'Подтверждено TikTok: {confirmed}', f'Ошибок адресатов: {failed}']
    if reason:
        lines.append(REASONS.get(reason, 'Подробности в журнале приложения.'))
    for item in results[:12]:
        name = str(item.get('target') or '')
        if name == '__run__':
            if item.get('error'):
                lines.append('Причина: ' + redact_secrets(str(item['error']))[:1500])
            continue
        if item.get('success') and item.get('confirmation') == 'server_ack':
            detail = 'отправлено; TikTok подтвердил'
            if item.get('reason'):
                detail += '. ' + REASONS.get(item['reason'], 'Ошибка записи состояния.')
        else:
            detail = REASONS.get(item.get('reason'), 'Отправка не подтверждена.')
        lines.append(f'• {name[:100]}: {detail}')
        if item.get('error'):
            lines.append('  Причина: ' + redact_secrets(str(item['error']))[:1500])
    if len(results) > 12:
        lines.append('Остальные адресаты — в журнале приложения.')
    return _telegram_text('\n'.join(lines), 4000)


def error_photos(adapter, summary: dict) -> list[tuple[Path, str]]:
    """Only this run's referenced screenshots inside this profile's artifacts."""
    photos = []
    seen = set()
    for item in summary.get('results') or []:
        raw = item.get('screenshot_path')
        if not raw or item.get('success'):
            continue
        try:
            folder = adapter._bot_profile_root(summary.get('profile_name')).resolve() / 'artifacts'
            path = Path(raw).resolve(strict=True)
            if (not path.is_relative_to(folder) or path.suffix.lower() != '.png' or not path.name.startswith('error_')
                    or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024 or path in seen):
                continue
            caption = f'Скриншот ошибки · {summary.get("profile_name") or "default"}'
            if item.get('target') != '__run__':
                caption += f'\nАдресат: {item.get("target") or "—"}'
            caption += '\n' + str(item.get('error') or REASONS.get(item.get('reason'), 'Ошибка обработки страницы.'))
            photos.append((path, _telegram_text(caption, 900)))
            seen.add(path)
            if len(photos) == 3:
                break
        except (AttributeError, OSError, ValueError):
            logger.warning('Скриншот ошибки недоступен; текстовый отчёт сохранён.')
    return photos


def notify_worker_result(adapter, summary: dict, *, status: str, reason: str = '', dry_run: bool = False) -> dict:
    if os.getenv('TG_DISABLE_NOTIFICATIONS', '0') == '1':
        return {'ok': False, 'reason': 'disabled'}
    try:
        setup = TelegramSetup(adapter)
        config = setup._read()
        if not config.get('token') or not config.get('notify_worker_results', True):
            return {'ok': False, 'reason': 'not_configured'}
        admins = parse_chat_ids(config.get('admin_chat_ids', config.get('allowed_chat_ids', [])))
        owners = parse_chat_ids((config.get('profile_owners') or {}).get(summary.get('profile_name'), []))
        ids = sorted(set(admins) | set(owners))
        text = report_text(summary, status=status, reason=reason, dry_run=dry_run)
        photos = error_photos(adapter, summary)
        sent = 0
        for chat_id in ids:
            try:
                result = setup._call('sendMessage', {'chat_id': chat_id, 'text': text})
                if isinstance(result, dict) and result.get('message_id'):
                    sent += 1
                    for path, caption in photos:
                        try:
                            attachment = setup._call('sendPhoto', {'chat_id': chat_id, 'caption': caption}, photo_path=path)
                            if not isinstance(attachment, dict) or not attachment.get('message_id'):
                                raise ValueError('Telegram не подтвердил получение фото.')
                        except Exception as exc:
                            logger.warning('Фото ошибки не доставлено; текст отправлен: %s', redact_secrets(str(exc)))
                else:
                    logger.warning('Telegram не подтвердил получение итогового уведомления')
            except Exception as exc:
                logger.warning('Итог Telegram не доставлен: %s', redact_secrets(str(exc)))
        if sent:
            logger.info('Итог запуска передан в Telegram: %s чатов; постоянный бот не запускался', sent)
        return {'ok': bool(ids) and sent == len(ids), 'sent_count': sent, 'failed_count': len(ids) - sent}
    except Exception as exc:
        logger.warning('Не удалось подготовить итог Telegram: %s', redact_secrets(str(exc)))
        return {'ok': False, 'reason': 'notification_error'}

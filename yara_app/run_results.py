"""User-visible outcomes. Process completion alone never proves a message was sent."""
from __future__ import annotations

from typing import Any

_VIEWS = {
    'accepted': ('TikTok принял сообщение', 'ok'),
    'partial': ('Отправлено не всё', 'warn'),
    'unknown': ('Не удалось подтвердить отправку', 'warn'),
    'rejected': ('TikTok отклонил отправку', 'bad'),
    'failed': ('Отправка не выполнена', 'bad'),
    'skipped': ('Отправка не начиналась', 'info'),
    'stopped': ('Отправка остановлена', 'info'),
    'running': ('Выполняется отправка', 'info'),
    'idle': ('Отправок пока нет', 'info'),
    'finished': ('Запуск завершён', 'info'),
}


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def outcome(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get('results')
    results = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    confirmed = sum(r.get('success') is True and r.get('confirmation') == 'server_ack' for r in results)
    if record.get('confirmation') == 'server_ack' and record.get('success') is True:
        confirmed = max(confirmed, 1)
    # New aggregate history records explicitly carry the count of receipts.
    confirmed = max(confirmed, _count(record.get('confirmed_count')))
    reason = str(record.get('reason') or '')
    status = str(record.get('status') or '')
    event = str(record.get('event') or '')
    claimed = max(_count(record.get('success_count')), int(record.get('success') is True))
    unknown = reason in {'delivery_unconfirmed', 'send_failed'} or claimed > confirmed or any(
        r.get('reason') in {'delivery_unconfirmed', 'send_failed'}
        or r.get('success') is True and r.get('confirmation') != 'server_ack' for r in results)
    if status in {'starting', 'running'} or event == 'run_started':
        code = 'running'
    elif unknown:
        code = 'unknown'
    elif confirmed:
        code = 'partial' if record.get('failed_count') or status == 'partial' else 'accepted'
    elif reason == 'send_rejected':
        code = 'rejected'
    elif status in {'stopped', 'paused'} or event == 'run_stopped' or reason in {'stop_requested', 'paused'}:
        code = 'stopped'
    elif record.get('skipped') or status == 'skipped' or event == 'run_skipped':
        code = 'skipped'
    elif record.get('success') is False or record.get('failed_count') or status == 'failed' or event == 'run_failed':
        code = 'failed'
    elif event == 'run_finished':
        code = 'finished'
    else:
        code = 'idle'
    label, kind = _VIEWS[code]
    return {'code': code, 'label': label, 'kind': kind, 'confirmed_count': confirmed}

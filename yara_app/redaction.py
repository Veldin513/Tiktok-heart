"""Redact authentication material before logs, diagnostics and error responses."""
from __future__ import annotations

import logging
import re

_TOKEN = re.compile(r'(?i)(?:/bot)?\d{5,16}:[A-Za-z0-9_-]{20,}')
_COOKIE = re.compile(r'(?i)\b(sessionid(?:_ss)?|sid_tt|sid_guard|ssid_ucp_v1|sid_ucp_v1|uid_tt(?:_ss)?|tt_csrf_token|access_key|msToken|ttwid)=([^\s;\"\x27&]+)')
_SECRET_KEYS = {'token', 'tg_token', 'cookies', 'cookie', 'authorization', 'password', 'sessionid', 'phrase', 'recovery_phrase', 'master_key',
                'sessionid_ss', 'access_key', 'mstoken', 'tt_csrf_token', 'sid_tt'}


def redact_secrets(text: str) -> str:
    return _COOKIE.sub(r'\1=<redacted>', _TOKEN.sub('<telegram-token-redacted>', text))


def redact_value(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: '<redacted>' if str(key).lower() in _SECRET_KEYS and isinstance(item, (str, list, dict)) else redact_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))

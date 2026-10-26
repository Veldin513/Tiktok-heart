"""Safe Telegram errors with enough detail to diagnose failed buttons."""
from __future__ import annotations

from yara_app.redaction import redact_secrets


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str):
        self.method = method
        self.code = code
        self.description = redact_secrets(description)[:500]
        super().__init__(f'{method}: HTTP {code}: {self.description}')

    @property
    def unchanged(self) -> bool:
        return self.method == 'editMessageText' and 'message is not modified' in self.description.lower()

    @property
    def expired_callback(self) -> bool:
        return self.method == 'answerCallbackQuery' and self.code == 400 and any(
            item in self.description.lower() for item in ('query is too old', 'query id is invalid', 'query_id_invalid'))

    def user_message(self) -> str:
        if self.code in {401, 404}:
            return 'Telegram не принял токен бота. Проверьте его в настройках приложения.'
        if self.code == 403:
            return 'Бот не может написать в этот чат. Разблокируйте его и нажмите «Начать».'
        if self.code == 429:
            return 'Нажатия приходят слишком часто. Подождите немного и повторите.'
        return 'Telegram не принял запрос. Откройте главное меню и повторите действие.'

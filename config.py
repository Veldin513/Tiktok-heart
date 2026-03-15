
from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BOT_NAME = 'tiktok_heart_bot'

# Browser/runtime defaults
DEFAULT_COOLDOWN_HOURS = 12
WORK_BROWSER_HEADLESS = True
AUTH_BROWSER_HEADLESS = False
ENABLE_WORK_BROWSER_ROUTING = False

# Telegram
TG_TOKEN = os.getenv('TG_TOKEN', '')
TG_CHAT_IDS = [cid.strip() for cid in os.getenv('TG_CHAT_IDS', '').split(',') if cid.strip()]
TG_ALLOWED_CHAT_IDS = [cid.strip() for cid in os.getenv('TG_ALLOWED_CHAT_IDS', ','.join(TG_CHAT_IDS)).split(',') if cid.strip()]
TG_DISABLE_NOTIFICATIONS = os.getenv('TG_DISABLE_NOTIFICATIONS', '0') == '1'

# Files
CONTROL_DIR = BASE_DIR / 'control'
PROFILES_FILE = CONTROL_DIR / 'profiles.json'
CONTROL_STATE_FILE = CONTROL_DIR / 'control_state.json'
MESSAGE_POOL_FILE = BASE_DIR / 'message_pool.txt'

# Defaults used to seed file-based stores
DEFAULT_PROFILE_NAME = 'default'
DEFAULT_PROFILES = {
    'default': [{'name': 'sample_target', 'url': '@sample_target'}],
}

DEFAULT_MESSAGES = [
    '❤️','💖','💕','💘','💌','🧡','💓','💗','💞','💝','💟','❣️','💛','💚','💙','💜',
    '❤️❤️','💖💖','💕💕','💘💘','💞💝','💓💞','💖💕','💌💖','💗💗','🧡🧡',
]


def get_cli_profile(argv: list[str] | None = None) -> str | None:
    args = argv or sys.argv
    return args[1] if len(args) > 1 else None


def load_message_variants() -> list[str]:
    try:
        if MESSAGE_POOL_FILE.exists():
            lines = [line.strip() for line in MESSAGE_POOL_FILE.read_text(encoding="utf-8").splitlines()]
            lines = [line for line in lines if line and not line.startswith("#")]
            out = []
            seen = set()
            for item in lines:
                if item not in seen:
                    seen.add(item)
                    out.append(item)
            if out:
                return out
    except Exception:
        pass
    return list(DEFAULT_MESSAGES)


MESSAGE_VARIANTS = load_message_variants()


@dataclass
class MessageSelector:
    """Return messages in a shuffled bag order.

    The selector keeps the historically simple API used by the worker, but it
    avoids excessive repeats inside one run by consuming a shuffled bag. When
    the bag is exhausted, it is refilled and shuffled again. If possible, the
    first message of the new bag is rotated away from the previous message so
    identical back-to-back sends become less likely.
    """

    messages: list[str]
    _bag: list[str] = field(default_factory=list)
    _last_message: str | None = None

    def next(self) -> str:
        if not self.messages:
            raise ValueError('Message pool is empty')

        if not self._bag:
            self._bag = list(self.messages)
            random.shuffle(self._bag)
            if self._last_message and len(self._bag) > 1 and self._bag[0] == self._last_message:
                self._bag.append(self._bag.pop(0))

        message = self._bag.pop(0)
        self._last_message = message
        return message

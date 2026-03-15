from __future__ import annotations

import importlib
import sys


def load_tiktok_checker_module(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['tiktok_checker.py'])
    if 'tiktok_checker' in sys.modules:
        del sys.modules['tiktok_checker']
    return importlib.import_module('tiktok_checker')


class DummyAdapter:
    def __init__(self, *, running: bool = False, ready: bool = True) -> None:
        self.running = running
        self.ready = ready
        self.stop_calls = 0
        self.start_calls = 0

    def get_telegram_bot_status(self):
        return type('Status', (), {'running': self.running})()

    def stop_telegram_bot(self, timeout: float = 8.0):  # noqa: ARG002
        self.stop_calls += 1
        self.running = False

    def telegram_bot_ready(self):
        return self.ready, 'ok' if self.ready else 'not ready'

    def start_telegram_bot(self):
        self.start_calls += 1
        self.running = True



def test_suspend_control_bot_for_auth_stops_running_bot(monkeypatch) -> None:
    module = load_tiktok_checker_module(monkeypatch)
    adapter = DummyAdapter(running=True)
    monkeypatch.setattr(module, 'auth_coord_adapter', adapter)

    result = module._suspend_control_bot_for_auth()

    assert result is True
    assert adapter.stop_calls == 1



def test_resume_control_bot_after_auth_restarts_when_ready(monkeypatch) -> None:
    module = load_tiktok_checker_module(monkeypatch)
    adapter = DummyAdapter(running=False, ready=True)
    monkeypatch.setattr(module, 'auth_coord_adapter', adapter)

    module._resume_control_bot_after_auth(True)

    assert adapter.start_calls == 1

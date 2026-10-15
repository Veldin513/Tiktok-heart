from __future__ import annotations

from pathlib import Path

from yara_app import single_instance
from yara_app.single_instance import SingleInstanceGuard


class _FakeProcess:
    def __init__(self, *, name: str, exe: str = '', cmdline: list[str] | None = None) -> None:
        self._name = name
        self._exe = exe
        self._cmdline = cmdline or []

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return self._exe

    def cmdline(self) -> list[str]:
        return self._cmdline


def test_single_instance_guard_prevents_second_acquire(tmp_path: Path) -> None:
    lock = tmp_path / '.app.lock'
    first = SingleInstanceGuard.acquire(lock, app_name='test-app')
    assert first is not None
    second = SingleInstanceGuard.acquire(lock, app_name='test-app')
    assert second is None
    first.release()
    third = SingleInstanceGuard.acquire(lock, app_name='test-app')
    assert third is not None
    third.release()


def test_app_shell_lock_rejects_reused_unrelated_pid() -> None:
    assert not single_instance._process_matches_app(
        _FakeProcess(name='svchost.exe'),
        'app_shell_server',
    )
    assert single_instance._process_matches_app(
        _FakeProcess(name='python.exe', cmdline=['python', 'app_shell/server.py']),
        'app_shell_server',
    )

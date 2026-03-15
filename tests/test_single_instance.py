from __future__ import annotations

from pathlib import Path

from single_instance import SingleInstanceGuard


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

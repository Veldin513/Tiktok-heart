from __future__ import annotations

import time
from pathlib import Path

import pytest

from yara_app.scheduler import WorkerScheduleService


class FakeAdapter:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.worker_schedule_register_script_path = base_dir / "register.ps1"
        self.worker_schedule_unregister_script_path = base_dir / "unregister.ps1"
        self.calls: list[tuple[Path, list[str]]] = []

    def _run_powershell_script(self, script_path: Path, args: list[str]):
        self.calls.append((script_path, args))


def test_schedule_service_registers_expected_trigger_arguments(tmp_path: Path, monkeypatch) -> None:
    service = WorkerScheduleService(FakeAdapter(tmp_path), task_name="Worker Test")
    monkeypatch.setattr(service, "status", lambda: {"installed": True, "task_name": "Worker Test"})

    result = service.register(at_startup=True, at_logon=False, every_12_hours=True, first_run_at="2026-07-21 00:02:18")

    assert result["installed"] is True
    script_path, args = service.adapter.calls[0]
    assert script_path == tmp_path / "register.ps1"
    assert args == [
        "-TaskName",
        "Worker Test",
        "-StartupTaskName",
        "Worker Test Startup",
        "-AtStartup",
        "true",
        "-AtLogon",
        "false",
        "-Every12Hours",
        "true",
        "-IntervalHours",
        "12",
        "-FirstRunAt",
        "2026-07-21 00:02:18",
    ]


def test_schedule_service_rejects_empty_trigger_set(tmp_path: Path) -> None:
    service = WorkerScheduleService(FakeAdapter(tmp_path))

    with pytest.raises(ValueError):
        service.register(at_startup=False, at_logon=False, every_12_hours=False)


def test_schedule_save_access_denied_reports_failure_without_querying_success(tmp_path, monkeypatch):
    service = WorkerScheduleService(FakeAdapter(tmp_path))
    def denied(*_args):
        raise RuntimeError('Register-ScheduledTask : Отказано в доступе. 0x80070005')
    monkeypatch.setattr(service.adapter, '_run_powershell_script', denied)
    monkeypatch.setattr(service, 'status', lambda: pytest.fail('failed registration is not success'))
    with pytest.raises(RuntimeError, match='Сохранение расписания не завершено'):
        service.register()


def test_schedule_disabled_does_not_get_recreated(tmp_path, monkeypatch):
    service = WorkerScheduleService(FakeAdapter(tmp_path))
    monkeypatch.setattr(service, 'status_cached', lambda **_kwargs: {'enabled': False})
    assert service.reanchor_from_queue() == {'updated': False, 'reason': 'repeat_disabled'}
    assert service.adapter.calls == []


def test_schedule_service_marks_stale_next_run(tmp_path: Path, monkeypatch) -> None:
    service = WorkerScheduleService(FakeAdapter(tmp_path))
    now = time.mktime(time.strptime("2026-07-20 12:00:00", "%Y-%m-%d %H:%M:%S"))
    monkeypatch.setattr("yara_app.scheduler.time.time", lambda: now)

    status = service.normalize_status({
        "installed": True,
        "enabled": True,
        "every_12_hours": True,
        "next_run_time": "2026-07-15 17:44:01",
    })

    assert status["next_run_stale"] is True
    assert status["schedule_warning"] == "next_run_time_is_in_the_past"


def test_schedule_service_merges_repeat_and_startup_tasks(tmp_path: Path) -> None:
    service = WorkerScheduleService(FakeAdapter(tmp_path), task_name="Worker Test")

    status = service._merge_task_statuses(
        {
            "installed": True,
            "enabled": True,
            "task_name": "Worker Test",
            "at_startup": False,
            "at_logon": False,
            "every_12_hours": True,
            "next_run_time": "2026-07-23 04:03:59",
            "last_run_time": "2026-07-22 02:59:10",
            "last_task_result": "0",
            "triggers": [{"text": "One Time Only, Hourly", "interval": "12 Hour(s), 0 Minute(s)"}],
        },
        {
            "installed": True,
            "enabled": True,
            "task_name": "Worker Test Startup",
            "at_startup": False,
            "at_logon": True,
            "every_12_hours": False,
            "last_run_time": "2026-07-22 16:03:57",
            "last_task_result": "0",
            "triggers": [{"text": "At logon", "interval": ""}],
        },
    )

    assert status["installed"] is True
    assert status["repeat_task_installed"] is True
    assert status["startup_task_installed"] is True
    assert status["at_startup"] is True
    assert status["at_logon"] is True
    assert status["every_12_hours"] is True
    assert status["next_run_time"] == "2026-07-23 04:03:59"
    assert status["last_run_time"] == "2026-07-22 16:03:57"
    assert len(status["triggers"]) == 2

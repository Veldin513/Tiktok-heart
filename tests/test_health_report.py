from __future__ import annotations

from pathlib import Path

from yara_app.project_adapter import ProjectAdapter


def _healthy_browser() -> dict[str, object]:
    return {
        "exists": True,
        "default_profile_exists": True,
        "preferences_exists": True,
        "cookies_exists": True,
        "needs_recovery": False,
        "auth_backoff_left": 0,
    }


def _flatten_text(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_text(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_text(item))
        return result
    return []


def test_health_marks_dry_run_without_treating_scheduled_worker_as_broken(tmp_path: Path) -> None:
    health = ProjectAdapter(tmp_path)._build_health(
        worker_running=False,
        telegram_running=False,
        telegram_ready=True,
        message_pool={"unique_count": 12},
        profiles=[object()],
        state={"dry_run": True, "paused": False, "stop_requested": False},
        preflight={"issues": []},
        browser_profile=_healthy_browser(),
        worker_schedule={"available": True, "installed": True, "next_run_time": "2026-04-30 04:58:15"},
        chrome_profiles=[{"id": "Default"}],
    )

    assert health["status"] == "warning"
    assert health["score"] >= 90
    assert any(item["key"] == "worker" and item["severity"] == "ok" for item in health["signals"])
    assert any(item["key"] == "mode" and item["severity"] == "warning" for item in health["signals"])


def test_health_text_is_not_mojibake(tmp_path: Path) -> None:
    health = ProjectAdapter(tmp_path)._build_health(
        worker_running=False,
        telegram_running=False,
        telegram_ready=True,
        message_pool={"unique_count": 12},
        profiles=[object()],
        state={"dry_run": False, "paused": False, "stop_requested": False},
        preflight={"issues": []},
        browser_profile=_healthy_browser(),
        worker_schedule={"available": True, "installed": True},
        chrome_profiles=[{"id": "Default"}],
    )

    text = "\n".join(_flatten_text(health))
    assert "Рќ" not in text
    assert "Рџ" not in text
    assert "Р—" not in text
    assert "РІ" not in text
    assert "СЃ" not in text
    assert "вЂ" not in text


def test_health_reports_reinstall_damaged_browser_profile_as_critical(tmp_path: Path) -> None:
    browser = _healthy_browser()
    browser["needs_recovery"] = True

    health = ProjectAdapter(tmp_path)._build_health(
        worker_running=False,
        telegram_running=False,
        telegram_ready=True,
        message_pool={"unique_count": 12},
        profiles=[object()],
        state={"dry_run": False, "paused": False, "stop_requested": False},
        preflight={"issues": []},
        browser_profile=browser,
        worker_schedule={"available": True, "installed": True},
        chrome_profiles=[{"id": "Default"}],
    )

    assert health["status"] == "critical"
    assert any(item["key"] == "browser" and item["severity"] == "critical" for item in health["signals"])


def test_schedule_normalization_marks_past_next_run(tmp_path: Path, monkeypatch) -> None:
    adapter = ProjectAdapter(tmp_path)
    now = adapter._parse_schedule_timestamp("2026-07-20 12:00:00")
    monkeypatch.setattr("yara_app.scheduler.time.time", lambda: now)

    status = adapter._normalize_worker_schedule_status(
        {
            "installed": True,
            "enabled": True,
            "every_12_hours": True,
            "next_run_time": "2026-07-15 17:44:01",
        }
    )

    assert status["next_run_stale"] is True
    assert status["schedule_warning"] == "next_run_time_is_in_the_past"


def test_health_warns_when_schedule_next_run_is_stale(tmp_path: Path) -> None:
    health = ProjectAdapter(tmp_path)._build_health(
        worker_running=False,
        telegram_running=False,
        telegram_ready=True,
        message_pool={"unique_count": 12},
        profiles=[object()],
        state={"dry_run": False, "paused": False, "stop_requested": False},
        preflight={"issues": []},
        browser_profile=_healthy_browser(),
        worker_schedule={
            "available": True,
            "installed": True,
            "next_run_time": "2026-07-15 17:44:01",
            "next_run_stale": True,
        },
        chrome_profiles=[{"id": "Default"}],
    )

    assert health["status"] == "warning"
    assert any(item["key"] == "schedule" and item["severity"] == "warning" for item in health["signals"])

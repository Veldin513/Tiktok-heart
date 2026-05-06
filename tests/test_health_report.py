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
    assert health["label"] == "Нужно внимание"
    assert health["score"] >= 90
    assert any(item["key"] == "worker" and item["severity"] == "ok" for item in health["signals"])
    assert any(item["key"] == "mode" and item["severity"] == "warning" for item in health["signals"])


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
    assert health["label"] == "Есть блокер"
    assert any(item["key"] == "browser" and item["severity"] == "critical" for item in health["signals"])

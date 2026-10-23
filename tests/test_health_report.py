from __future__ import annotations

from pathlib import Path

import pytest

from yara_app.project_adapter import ProjectAdapter


def _healthy_browser() -> dict[str, object]:
    return {
        "exists": True,
        "default_profile_exists": True,
        "preferences_exists": True,
        "cookies_exists": True,
        "needs_recovery": False,
        "auth_backoff_left": 0,
        "local_state_exists": True,
        "session_validation": {"valid": True},
    }


@pytest.mark.parametrize('change', [
    {'session_import_required': True},
    {'session_validation': {'valid': False}},
])
def test_health_does_not_mark_cookie_file_as_valid_login(tmp_path, change):
    browser = {**_healthy_browser(), **change}
    health = ProjectAdapter(tmp_path)._build_health(
        False, False, True, {'unique_count': 12}, [object()], {}, {'issues': []}, browser, {'installed': True},
    )
    assert health['status'] == 'critical'
    assert health['score'] <= 64
    assert not any(item['key'] == 'browser' and item['severity'] == 'ok' for item in health['signals'])


def test_existing_cookies_without_validation_are_unknown(tmp_path):
    browser = {**_healthy_browser(), 'session_validation': {'valid': None}}
    health = ProjectAdapter(tmp_path)._build_health(
        False, False, True, {'unique_count': 12}, [object()], {}, {'issues': []}, browser, {'installed': True},
    )
    assert health['status'] == 'ok'
    assert any(item['key'] == 'browser' and item['state'] == 'Не проверена' for item in health['signals'])


@pytest.mark.parametrize('valid', [True, None])
def test_missing_metadata_alone_does_not_block_working_profile(tmp_path, valid):
    browser = {**_healthy_browser(), 'local_state_exists': False, 'preferences_exists': False,
               'session_validation': {'valid': valid}}
    health = ProjectAdapter(tmp_path)._build_health(
        False, False, True, {'unique_count': 12}, [object()], {}, {'issues': []}, browser, {'installed': True},
    )
    assert health['status'] == 'ok'
    assert health['score'] == 100
    assert not any(item['severity'] in ('warning', 'critical') for item in health['issues'])
    assert any(item['key'] == 'profile_metadata' and item['severity'] == 'info' for item in health['signals'])
    assert any(item['key'] == 'browser' and item['severity'] == ('ok' if valid else 'info') for item in health['signals'])


def test_live_and_full_browser_status_preserve_validation_marker(tmp_path):
    import json
    import time

    adapter = ProjectAdapter(tmp_path)
    state_dir = tmp_path / 'profiles' / 'alpha' / 'state'
    state_dir.mkdir(parents=True)
    (state_dir / 'session_import_required.json').write_text('{}')
    (state_dir / 'session_validation.json').write_text(json.dumps({'valid': False, 'checked_at': time.time()}))
    for data in [adapter.browser_profile_summary('alpha'), adapter.browser_profile_live_status('alpha')]:
        assert data['session_import_required'] is True
        assert data['session_validation']['valid'] is False


def test_log_zero_success_count_cannot_hide_a_later_error(tmp_path):
    path = tmp_path / 'worker.log'
    path.write_text('INFO success_count=1\nERROR auth failed\nINFO success_count=0\n', encoding='utf-8')
    summary = ProjectAdapter(tmp_path)._log_summary(path)
    assert summary['successes'] == 1
    assert summary['latest_result'] == 'error'


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

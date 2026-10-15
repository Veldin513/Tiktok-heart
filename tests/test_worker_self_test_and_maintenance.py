from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.start_worker_once import configure_boot_force_environment, run_self_test, wait_for_near_cooldown
from yara_app.project_adapter import ProjectAdapter


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_clean_project_caches_removes_only_project_cache_dirs(tmp_path: Path) -> None:
    cache_dir = tmp_path / "yara_app" / "module" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "x.pyc").write_bytes(b"cache")
    root_cache = tmp_path / ".pytest_cache"
    root_cache.mkdir()
    (root_cache / "README.md").write_text("cache", encoding="utf-8")
    ignored_profile_cache = tmp_path / "profiles" / "alpha" / "__pycache__"
    ignored_profile_cache.mkdir(parents=True)
    (ignored_profile_cache / "keep.pyc").write_bytes(b"keep")

    result = ProjectAdapter(tmp_path).clean_project_caches()

    assert result["removed_count"] == 2
    assert result["freed_bytes"] >= 10
    assert not cache_dir.exists()
    assert not root_cache.exists()
    assert ignored_profile_cache.exists()


def test_run_maintenance_reports_browser_compact_skip_when_profile_missing(tmp_path: Path) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": []})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha"})

    result = ProjectAdapter(tmp_path).run_maintenance("alpha")

    assert result["profile_key"] == "alpha"
    assert any(item["name"] == "project_caches" and item["ok"] for item in result["actions"])
    assert any(item["name"] == "browser_compact" and not item["ok"] for item in result["actions"])


def test_app_metadata_prefers_package_version(tmp_path: Path) -> None:
    write_json(tmp_path / "package.json", {"version": "2.0.0"})
    write_json(tmp_path / "BUILD_INFO.json", {"build": "test-build", "notes": ["note"]})

    meta = ProjectAdapter(tmp_path).app_metadata()

    assert meta["version"] == "2.0.0"
    assert meta["build"] == "test-build"


def test_worker_self_test_can_skip_browser_probe(tmp_path: Path) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": [{"name": "target", "url": "@target"}]})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha", "cooldown_hours": 12})
    (tmp_path / "message_pool.txt").write_text("hello\n", encoding="utf-8")
    main_script = tmp_path / "yara_app" / "tiktok_checker.py"
    main_script.parent.mkdir(parents=True)
    main_script.write_text("print('ok')\n", encoding="utf-8")

    result = run_self_test(ProjectAdapter(tmp_path), browser=False)

    assert result["safe_mode"] == "no_send"
    assert result["browser_probe"]["enabled"] is False
    assert result["queue"]["target_count"] == 1
    assert result["message_count"] == 1


def test_worker_starter_waits_for_near_cooldown(monkeypatch) -> None:
    class FakeAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def queue_snapshot(self) -> dict:
            self.calls += 1
            return {
                "target_count": 1,
                "ready_count": 1,
                "cooldown_count": 0,
                "next_send_in_s": 0,
            }

    slept: list[int] = []
    monkeypatch.setenv("WORKER_COOLDOWN_GRACE_SECONDS", "30")
    refreshed, waited = wait_for_near_cooldown(
        FakeAdapter(),
        {
            "target_count": 1,
            "ready_count": 0,
            "cooldown_count": 1,
            "next_target": "target",
            "next_send_in_s": 14,
        },
        sleep=slept.append,
    )

    assert waited is True
    assert slept == [16]
    assert refreshed["ready_count"] == 1


def test_worker_starter_does_not_wait_for_far_cooldown(monkeypatch) -> None:
    class FakeAdapter:
        def queue_snapshot(self) -> dict:
            raise AssertionError("far cooldown should not be rechecked")

    monkeypatch.setenv("WORKER_COOLDOWN_GRACE_SECONDS", "30")
    queue = {
        "target_count": 1,
        "ready_count": 0,
        "cooldown_count": 1,
        "next_send_in_s": 600,
    }
    refreshed, waited = wait_for_near_cooldown(FakeAdapter(), queue, sleep=lambda _seconds: None)

    assert waited is False
    assert refreshed is queue


def test_worker_starter_sets_boot_force_for_recent_startup(monkeypatch) -> None:
    monkeypatch.delenv("WORKER_FORCE_SEND_ON_BOOT", raising=False)
    monkeypatch.setenv("WORKER_BOOT_FORCE_WINDOW_SECONDS", "900")
    monkeypatch.setattr("scripts.start_worker_once.system_uptime_seconds", lambda: 120)

    enabled = configure_boot_force_environment()

    assert enabled is True
    assert os.environ["WORKER_FORCE_SEND_ON_BOOT"] == "1"


def test_worker_starter_uses_boot_force_once_per_windows_boot(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": [{"name": "target", "url": "@target"}]})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha", "cooldown_hours": 12})
    adapter = ProjectAdapter(tmp_path)

    monkeypatch.setenv("WORKER_BOOT_FORCE_WINDOW_SECONDS", "900")
    monkeypatch.setattr("scripts.start_worker_once.system_uptime_seconds", lambda: 120)
    monkeypatch.delenv("WORKER_FORCE_SEND_ON_BOOT", raising=False)

    assert configure_boot_force_environment(adapter) is True
    assert os.environ["WORKER_FORCE_SEND_ON_BOOT"] == "1"

    monkeypatch.delenv("WORKER_FORCE_SEND_ON_BOOT", raising=False)
    assert configure_boot_force_environment(adapter) is False
    assert "WORKER_FORCE_SEND_ON_BOOT" not in os.environ


def test_worker_starter_forced_boot_bypass_ignores_uptime_window(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": [{"name": "target", "url": "@target"}]})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha", "cooldown_hours": 12})
    adapter = ProjectAdapter(tmp_path)

    monkeypatch.setenv("WORKER_BOOT_FORCE_WINDOW_SECONDS", "900")
    monkeypatch.setattr("scripts.start_worker_once.system_uptime_seconds", lambda: 7200)
    monkeypatch.delenv("WORKER_FORCE_SEND_ON_BOOT", raising=False)

    assert configure_boot_force_environment(adapter, force=True) is True
    assert os.environ["WORKER_FORCE_SEND_ON_BOOT"] == "1"

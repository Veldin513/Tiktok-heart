from __future__ import annotations

import os
from pathlib import Path

import yara_app.runtime_paths as runtime_paths
from yara_app.runtime_paths import ProfilePaths, RunLock, recover_browser_profile_after_reinstall


def test_run_lock_recovers_stale_pid(tmp_path: Path, monkeypatch) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "_process_is_running", lambda _pid: False)

    lock = RunLock(lock_path)

    assert lock.acquire() is True
    assert lock.acquired is True
    assert lock_path.read_text(encoding="utf-8") == str(os.getpid())


def test_run_lock_keeps_active_pid(tmp_path: Path, monkeypatch) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("1234", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "_process_is_running", lambda _pid: True)

    assert RunLock(lock_path).acquire() is False
    assert lock_path.read_text(encoding="utf-8") == "1234"


def test_run_lock_recovers_malformed_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("not-a-pid", encoding="utf-8")

    lock = RunLock(lock_path)

    assert lock.acquire() is True
    assert lock_path.read_text(encoding="utf-8") == str(os.getpid())


def test_recover_browser_profile_moves_incompatible_user_data(tmp_path: Path, monkeypatch) -> None:
    paths = _profile_paths(tmp_path)
    paths.user_data_dir.mkdir(parents=True)
    (paths.user_data_dir / "Local State").write_text("old", encoding="utf-8")
    (paths.user_data_dir / "Default").mkdir()

    monkeypatch.setattr(runtime_paths, "browser_profile_needs_recovery", lambda path: path == paths.user_data_dir)

    backup_dir = recover_browser_profile_after_reinstall(paths)

    assert backup_dir is not None
    assert backup_dir.exists()
    assert backup_dir.parent == paths.browser_dir
    assert (backup_dir / "Local State").read_text(encoding="utf-8") == "old"
    assert (backup_dir / "Default").is_dir()
    assert paths.user_data_dir.exists()
    assert not (paths.user_data_dir / "Local State").exists()


def _profile_paths(root: Path) -> ProfilePaths:
    profile_root = root / "profiles" / "alpha"
    browser_dir = profile_root / "browser"
    user_data_dir = browser_dir / "user_data"
    logs_dir = profile_root / "logs"
    state_dir = profile_root / "state"
    artifacts_dir = profile_root / "artifacts"
    for path in (browser_dir, logs_dir, state_dir, artifacts_dir):
        path.mkdir(parents=True, exist_ok=True)
    return ProfilePaths(
        profile_name="alpha",
        profile_root=profile_root,
        browser_dir=browser_dir,
        user_data_dir=user_data_dir,
        logs_dir=logs_dir,
        state_dir=state_dir,
        artifacts_dir=artifacts_dir,
        run_lock_file=state_dir / "run.lock",
    )

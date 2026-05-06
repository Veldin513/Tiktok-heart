from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from yara_app.project_adapter import ProjectAdapter


def _write(path: Path, text: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_auth_backup_includes_authorization_files_and_skips_heavy_caches(tmp_path: Path) -> None:
    _write(tmp_path / "profiles" / "main" / "browser" / "user_data" / "Local State")
    _write(tmp_path / "profiles" / "main" / "browser" / "user_data" / "Default" / "Network" / "Cookies")
    _write(tmp_path / "profiles" / "main" / "browser" / "user_data" / "Default" / "Local Storage" / "leveldb" / "auth.ldb")
    _write(tmp_path / "profiles" / "main" / "browser" / "user_data" / "Default" / "Cache" / "data_0")
    _write(tmp_path / "profiles" / "main" / "state" / "last_send_sample.txt", "1000")
    _write(tmp_path / "control" / "profiles.json", json.dumps({"main": []}))
    _write(tmp_path / "control" / "telegram_bot_v2.json", json.dumps({"token": "local-token"}))
    _write(tmp_path / "message_pool.txt", "hello")

    result = ProjectAdapter(tmp_path).create_auth_backup("main")

    assert result["error_count"] == 0
    with zipfile.ZipFile(result["path"]) as archive:
        names = set(archive.namelist())
    assert "BACKUP_MANIFEST.json" in names
    assert "profiles/main/browser/user_data/Local State" in names
    assert "profiles/main/browser/user_data/Default/Network/Cookies" in names
    assert "profiles/main/browser/user_data/Default/Local Storage/leveldb/auth.ldb" in names
    assert "profiles/main/state/last_send_sample.txt" in names
    assert "control/profiles.json" in names
    assert "control/telegram_bot_v2.json" in names
    assert "message_pool.txt" in names
    assert "profiles/main/browser/user_data/Default/Cache/data_0" not in names


def test_public_project_backup_excludes_runtime_private_files(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# App")
    _write(tmp_path / "app_shell" / "web" / "app.js", "console.log('ok')")
    _write(tmp_path / "control" / "control_state.example.json", "{}")
    _write(tmp_path / "control" / "control_state.json", "{}")
    _write(tmp_path / "profiles" / "main" / "browser" / "user_data" / "Default" / "Network" / "Cookies")
    _write(tmp_path / "logs" / "app.log", "private path")

    result = ProjectAdapter(tmp_path).create_public_project_backup()

    with zipfile.ZipFile(result["path"]) as archive:
        names = set(archive.namelist())
    assert "README.md" in names
    assert "app_shell/web/app.js" in names
    assert "control/control_state.example.json" in names
    assert "control/control_state.json" not in names
    assert not any(name.startswith("profiles/") for name in names)
    assert not any(name.startswith("logs/") for name in names)


def test_prune_auth_backups_keeps_latest_for_selected_profile(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups" / "auth"
    _write(backup_dir / "auth_backup_main_20260101_010101.zip", "old")
    _write(backup_dir / "auth_backup_main_20260102_010101.zip", "new")
    _write(backup_dir / "auth_backup_other_20260101_010101.zip", "other")
    os.utime(backup_dir / "auth_backup_main_20260101_010101.zip", (1000, 1000))
    os.utime(backup_dir / "auth_backup_main_20260102_010101.zip", (2000, 2000))

    result = ProjectAdapter(tmp_path).prune_auth_backups("main", keep_latest=1)

    assert result["removed_count"] == 1
    assert not (backup_dir / "auth_backup_main_20260101_010101.zip").exists()
    assert (backup_dir / "auth_backup_main_20260102_010101.zip").exists()
    assert (backup_dir / "auth_backup_other_20260101_010101.zip").exists()


def test_delete_selected_backups_is_limited_to_selected_profile(tmp_path: Path) -> None:
    auth_dir = tmp_path / "backups" / "auth"
    browser_dir = tmp_path / "profiles" / "main" / "browser"
    _write(auth_dir / "auth_backup_main_20260101_010101.zip", "auth")
    _write(auth_dir / "auth_backup_other_20260101_010101.zip", "other")
    _write(browser_dir / "user_data_before_chrome_import_20260101" / "Local State", "browser")
    _write(browser_dir / "user_data" / "Default" / "Network" / "Cookies", "cookies")

    result = ProjectAdapter(tmp_path).delete_selected_backups("main", [
        {"kind": "auth", "name": "auth_backup_main_20260101_010101.zip"},
        {"kind": "auth", "name": "auth_backup_other_20260101_010101.zip"},
        {"kind": "browser", "name": "user_data_before_chrome_import_20260101"},
        {"kind": "browser", "name": "user_data"},
    ])

    assert result["removed_count"] == 2
    assert result["skipped_count"] == 2
    assert not (auth_dir / "auth_backup_main_20260101_010101.zip").exists()
    assert (auth_dir / "auth_backup_other_20260101_010101.zip").exists()
    assert not (browser_dir / "user_data_before_chrome_import_20260101").exists()
    assert (browser_dir / "user_data").exists()

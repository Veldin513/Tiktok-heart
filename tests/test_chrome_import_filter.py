from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from yara_app.project_adapter import ProjectAdapter


def _write_cookie_db(path: Path, hosts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE cookies(host_key TEXT)")
        con.executemany("INSERT INTO cookies(host_key) VALUES (?)", [(host,) for host in hosts])
        con.commit()
    finally:
        con.close()


def _cookie_hosts(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [row[0] for row in con.execute("SELECT host_key FROM cookies ORDER BY host_key")]
    finally:
        con.close()


def test_filtered_tiktok_cookie_copy_keeps_only_tiktok_domains(tmp_path: Path) -> None:
    source_profile = tmp_path / "chrome" / "Default"
    cookie_path = source_profile / "Network" / "Cookies"
    _write_cookie_db(
        cookie_path,
        [".tiktok.com", "www.tiktok.com", "youtube.com", ".byteoversea.com", "example.com"],
    )

    destination_profile = tmp_path / "dest" / "Default"
    result = ProjectAdapter(tmp_path)._copy_filtered_tiktok_cookies(source_profile, destination_profile)

    assert result["copied"] is True
    assert result["before"] == 5
    assert result["kept"] == 3
    assert _cookie_hosts(destination_profile / "Network" / "Cookies") == [
        ".byteoversea.com",
        ".tiktok.com",
        "www.tiktok.com",
    ]


def test_tiktok_session_copy_skips_heavy_chrome_profile_dirs(tmp_path: Path) -> None:
    source_root = tmp_path / "Chrome" / "User Data"
    source_profile = source_root / "Default"
    source_profile.mkdir(parents=True)
    (source_root / "Local State").write_text("local-state", encoding="utf-8")
    (source_profile / "Preferences").write_text("prefs", encoding="utf-8")
    (source_profile / "Secure Preferences").write_text("secure", encoding="utf-8")
    _write_cookie_db(
        source_profile / "Network" / "Cookies",
        [".tiktok.com", "youtube.com", "login.tiktokv.com"],
    )

    for heavy_dir in ("Cache", "Code Cache", "Service Worker", "Extensions"):
        path = source_profile / heavy_dir / "payload.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 128)

    (source_profile / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb").mkdir(parents=True)
    (source_profile / "IndexedDB" / "https_www.youtube.com_0.indexeddb.leveldb").mkdir(parents=True)

    destination = tmp_path / "bot" / "browser" / "user_data"
    details = ProjectAdapter(tmp_path)._copy_tiktok_session_profile(source_root, source_profile, destination)

    assert details["cookies"]["kept"] == 2
    assert (destination / "Local State").read_text(encoding="utf-8") == "local-state"
    assert (destination / "Default" / "Preferences").read_text(encoding="utf-8") == "prefs"
    assert (destination / "Default" / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb").is_dir()
    assert not (destination / "Default" / "IndexedDB" / "https_www.youtube.com_0.indexeddb.leveldb").exists()
    for heavy_dir in ("Cache", "Code Cache", "Service Worker", "Extensions"):
        assert not (destination / "Default" / heavy_dir).exists()


def test_compact_browser_profile_removes_heavy_dirs_and_filters_existing_cookies(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "alpha" / "browser" / "user_data" / "Default"
    profile.mkdir(parents=True)
    _write_cookie_db(profile / "Network" / "Cookies", [".tiktok.com", "example.com", ".byteoversea.com"])

    for heavy_dir in ("Cache", "Code Cache", "Service Worker", "Extensions"):
        path = profile / heavy_dir / "payload.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 256)
    (profile / "IndexedDB" / "https_chatgpt.com_0.indexeddb.leveldb").mkdir(parents=True)
    (profile / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb").mkdir(parents=True)
    (profile / "Local Storage").mkdir()
    (profile / "Local Storage" / "keep.ldb").write_text("auth", encoding="utf-8")

    result = ProjectAdapter(tmp_path).compact_browser_profile("alpha")

    assert result["removed_count"] == 4
    assert result["freed_bytes"] == 1024
    assert _cookie_hosts(profile / "Network" / "Cookies") == [".byteoversea.com", ".tiktok.com"]
    assert not (profile / "IndexedDB" / "https_chatgpt.com_0.indexeddb.leveldb").exists()
    assert (profile / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb").exists()
    assert (profile / "Local Storage" / "keep.ldb").exists()
    for heavy_dir in ("Cache", "Code Cache", "Service Worker", "Extensions"):
        assert not (profile / heavy_dir).exists()


def test_browser_summary_counts_backup_size_and_prune_keeps_latest(tmp_path: Path) -> None:
    browser_dir = tmp_path / "profiles" / "alpha" / "browser"
    user_data = browser_dir / "user_data" / "Default"
    user_data.mkdir(parents=True)
    (user_data / "Preferences").write_text("prefs", encoding="utf-8")
    _write_cookie_db(user_data / "Network" / "Cookies", [".tiktok.com"])

    old_backup = browser_dir / "user_data_backup_20260401_010101"
    new_backup = browser_dir / "user_data_before_chrome_import_20260402_010101"
    old_backup.mkdir(parents=True)
    new_backup.mkdir(parents=True)
    (old_backup / "old.bin").write_bytes(b"x" * 100)
    (new_backup / "new.bin").write_bytes(b"x" * 200)
    os.utime(old_backup, (1000, 1000))
    os.utime(new_backup, (2000, 2000))

    adapter = ProjectAdapter(tmp_path)
    summary = adapter.browser_profile_summary("alpha")

    assert summary["backup_count"] == 2
    assert summary["backup_bytes"] == 300

    result = adapter.prune_browser_backups("alpha", keep_latest=1)

    assert result["removed_count"] == 1
    assert result["freed_bytes"] == 100
    assert not old_backup.exists()
    assert new_backup.exists()

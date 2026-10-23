from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from yara_app.project_adapter import ProjectAdapter, WorkerStatus


def _write(path: Path, text: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _prepare_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ProjectAdapter, Path]:
    adapter = ProjectAdapter(tmp_path)
    chrome_root = tmp_path / "chrome" / "User Data"
    _write(chrome_root / "Local State", "{}")
    _write(chrome_root / "Default" / "Preferences", "{}")
    monkeypatch.setattr(adapter, "_google_chrome_user_data_dir", lambda: chrome_root)
    monkeypatch.setattr(adapter, "_running_process_pids_by_name", lambda _names: [])
    monkeypatch.setattr(adapter, "get_worker_status", lambda: WorkerStatus(running=False, pid=None))
    return adapter, chrome_root


def _write_cookies(path: Path, domains: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE cookies (host_key TEXT)")
        database.executemany("INSERT INTO cookies(host_key) VALUES (?)", [(domain,) for domain in domains])


def test_session_import_rejects_profile_without_tiktok_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chrome_root = _prepare_adapter(tmp_path, monkeypatch)
    _write_cookies(chrome_root / "Default" / "Network" / "Cookies", ["example.com"])
    old_profile = tmp_path / "profiles" / "main" / "browser" / "user_data"
    _write(old_profile / "Local State", "old-profile")
    _write(old_profile / "Default" / "Preferences", "old-preferences")

    with pytest.raises(RuntimeError, match="авторизация TikTok"):
        adapter.import_google_chrome_profile(chrome_profile_id="Default", bot_profile_key="main")

    assert (old_profile / "Local State").read_text(encoding="utf-8") == "old-profile"
    assert not list(old_profile.parent.glob("user_data_import_tmp_*"))
    assert not list(old_profile.parent.glob("user_data_before_chrome_import_*"))


def test_session_import_validates_staging_and_creates_recovery_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chrome_root = _prepare_adapter(tmp_path, monkeypatch)
    _write_cookies(chrome_root / "Default" / "Network" / "Cookies", [".tiktok.com", "example.com"])
    old_profile = tmp_path / "profiles" / "main" / "browser" / "user_data"
    _write(old_profile / "Local State", "old-profile")
    _write(old_profile / "Default" / "Preferences", "old-preferences")
    _write(old_profile / "Default" / "Network" / "Cookies", "old-cookies")
    _write(tmp_path / "profiles" / "main" / "state" / "auth_backoff.json", '{"until": 9999999999}')

    result = adapter.import_google_chrome_profile(chrome_profile_id="Default", bot_profile_key="main")

    assert result["validation"]["ok"] is True
    assert result["validation"]["tiktok_cookies"] == 1
    assert result["auth_backoff_removed"] is False
    assert result["auth_backoff_preserved"] is True
    assert result["auth_backoff_reset_error"] == ""
    assert Path(result["backup"]).is_dir()
    assert (Path(result["backup"]) / "Local State").read_text(encoding="utf-8") == "old-profile"
    assert Path(result["pre_import_auth_backup"]["path"]).is_file()
    assert (tmp_path / "profiles" / "main" / "state" / "auth_backoff.json").exists()


def test_staged_browser_commit_restores_old_profile_when_promotion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ProjectAdapter(tmp_path)
    browser_dir = tmp_path / "profiles" / "main" / "browser"
    active = browser_dir / "user_data"
    staged = browser_dir / "user_data_import_tmp_test"
    _write(active / "Local State", "old-profile")
    _write(staged / "Local State", "new-profile")
    original_move = shutil.move

    def fail_promotion(source: str, destination: str, *args, **kwargs):
        if Path(source) == staged and Path(destination) == active:
            _write(active / "partial", "broken")
            raise OSError("promotion failed")
        return original_move(source, destination, *args, **kwargs)

    monkeypatch.setattr("yara_app.project_adapter.shutil.move", fail_promotion)

    with pytest.raises(OSError, match="promotion failed"):
        adapter._commit_staged_browser_profile(
            browser_dir=browser_dir,
            user_data_dir=active,
            staged_user_data_dir=staged,
            timestamp="20260729_142100",
        )

    assert (active / "Local State").read_text(encoding="utf-8") == "old-profile"
    assert not (active / "partial").exists()
    assert not list(browser_dir.glob("user_data_before_chrome_import_*"))


def test_protected_chrome_import_preserves_current_profile_and_pause(tmp_path, monkeypatch):
    adapter, chrome_root = _prepare_adapter(tmp_path, monkeypatch)
    cookies_path = chrome_root / 'Default/Network/Cookies'
    cookies_path.parent.mkdir(parents=True)
    with sqlite3.connect(cookies_path) as database:
        database.execute('CREATE TABLE cookies (host_key TEXT, encrypted_value BLOB)')
        database.execute('INSERT INTO cookies VALUES (?, ?)', ('.tiktok.com', b'v20protected'))
    old_profile = tmp_path / 'profiles/main/browser/user_data'
    _write(old_profile / 'Local State', 'old-profile')
    pause = tmp_path / 'profiles/main/state/auth_backoff.json'
    _write(pause, '{"until": 9999999999}')

    with pytest.raises(RuntimeError, match='v20'):
        adapter.import_google_chrome_profile(bot_profile_key='main')

    assert (old_profile / 'Local State').read_text() == 'old-profile'
    assert pause.exists()
    assert not list(old_profile.parent.glob('user_data_import_tmp_*'))


def test_unreadable_windows_cookie_key_rejects_import_without_replacing_profile(tmp_path, monkeypatch):
    adapter, chrome_root = _prepare_adapter(tmp_path, monkeypatch)
    _write_cookies(chrome_root / 'Default/Network/Cookies', ['.tiktok.com'])
    old_profile = tmp_path / 'profiles/main/browser/user_data'
    _write(old_profile / 'Local State', 'old-profile')
    monkeypatch.setattr('yara_app.project_adapter.browser_profile_needs_recovery', lambda _path: True)

    with pytest.raises(RuntimeError, match='Windows'):
        adapter.import_google_chrome_profile(bot_profile_key='main')

    assert (old_profile / 'Local State').read_text() == 'old-profile'
    assert not list(old_profile.parent.glob('user_data_import_tmp_*'))

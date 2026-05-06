from __future__ import annotations

import json
from pathlib import Path

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

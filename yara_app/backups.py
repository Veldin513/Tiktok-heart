from __future__ import annotations

import json
import shutil
import stat
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yara_app.project_adapter import ProjectAdapter

from yara_app.profile_vault import MAGIC, is_encrypted, protect_backup, protected_zip, vault_path


@contextmanager
def restore_staging(base: Path):
    parent = (base / 'backups').resolve()
    staging = Path(tempfile.mkdtemp(prefix='auth_restore_', dir=parent)).resolve()
    try:
        yield staging
    except Exception as exc:
        # Preserve originals even if the rollback itself meets an I/O error.
        raise RuntimeError(f'Восстановление отменено. Файлы для восстановления сохранены: {staging}') from exc
    else:
        if staging.parent != parent or not staging.name.startswith('auth_restore_'):
            raise ValueError('Unexpected restore staging directory')
        shutil.rmtree(staging)


class BackupService:
    """Owns source/auth backup and restore flows for ProjectAdapter."""

    def __init__(self, adapter: ProjectAdapter) -> None:
        self.adapter = adapter

    def auth_backup_files(self, profile_key: str) -> list[Path]:
        backup_dir = self.adapter.base_dir / "backups" / "auth"
        if not backup_dir.exists():
            return []
        return [
            path
            for path in backup_dir.iterdir()
            if path.is_file() and path.name.startswith(f"auth_backup_{profile_key}_") and path.suffix.lower() == ".zip"
        ]

    def auth_backup_by_name(self, profile_key: str, name: str) -> Path | None:
        backup_name = Path(str(name or "")).name
        if not backup_name:
            return None
        return {path.name: path for path in self.auth_backup_files(profile_key)}.get(backup_name)

    @staticmethod
    def safe_zip_name(name: str) -> bool:
        if not name or '\\' in name or ':' in name or '\x00' in name:
            return False
        try:
            pure = PurePosixPath(name)
        except Exception:
            return False
        if pure.is_absolute() or ".." in pure.parts:
            return False
        text = pure.as_posix()
        return bool(text and not text.startswith('/') and all(
            part and not part.endswith(('.', ' ')) for part in pure.parts))

    @staticmethod
    def auth_restore_policy(profile_key: str, *, include_global: bool = False) -> dict[str, tuple[str, ...]]:
        profile_prefix = f"profiles/{profile_key}/"
        allowed_profile = (
            f"{profile_prefix}browser/user_data/",
            f"{profile_prefix}state/",
            f"{profile_prefix}artifacts/",
            f"{profile_prefix}logs/",
        )
        allowed_global = (
            "message_pool.txt",
            ".env",
            ".env.local",
            "control/control_state.json",
            "control/profiles.json",
            "control/telegram_bot_v2.json",
            "control/ui_state.json",
        ) if include_global else ()
        return {"profile": allowed_profile, "global": allowed_global}

    def classify_auth_backup_member(
        self,
        member_name: str,
        profile_key: str,
        *,
        include_global: bool = False,
    ) -> str:
        if member_name == "BACKUP_MANIFEST.json":
            return "manifest"
        if not self.safe_zip_name(member_name):
            return "blocked"
        policy = self.auth_restore_policy(profile_key, include_global=include_global)
        if any(member_name.startswith(prefix) for prefix in policy["profile"]):
            return "profile"
        if member_name in policy["global"]:
            return "global"
        return "blocked"

    def validate_auth_backup(
        self,
        profile_key: str | None,
        backup_name: str,
        *,
        include_global: bool = False,
        deep: bool = False,
    ) -> dict[str, Any]:
        adapter = self.adapter
        profile_root = adapter._bot_profile_root(profile_key)
        backup = self.auth_backup_by_name(profile_root.name, backup_name)
        if backup is None:
            return {
                "ok": False,
                "profile_key": profile_root.name,
                "name": Path(str(backup_name or "")).name,
                "error": "Backup not found for selected profile",
            }

        manifest: dict[str, Any] = {}
        if not deep and is_encrypted(backup):
            # Rendering the browser card must not decrypt multi-gigabyte
            # backups. Full authentication/CRC checks happen before restore.
            with backup.open('rb') as stream:
                header = stream.read(len(MAGIC) + 32 + 12)
            return {'ok': len(header) == len(MAGIC) + 44 and backup.stat().st_size >= len(header) + 16,
                    'encrypted': True, 'verification': 'checked_before_restore', 'entry_count': 0,
                    'blocked_count': 0, 'manifest_profile': profile_root.name}
        entries: list[dict[str, Any]] = []
        blocked: list[str] = []
        profile_entries = 0
        global_entries = 0
        try:
            with protected_zip(self.adapter.base_dir, backup) as archive:
                infos = archive.infolist()
                if (len(infos) > 50000 or sum(info.file_size for info in infos) > 4 * 1024**3
                        or any(info.file_size > 1024**3 for info in infos)):
                    return {'ok': False, 'error': 'Архив превышает лимиты восстановления (50000 файлов, 4 ГБ).'}
                if any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
                    return {'ok': False, 'error': 'Архив содержит символические ссылки.'}
                if any(info.filename == 'BACKUP_MANIFEST.json' and info.file_size > 1024**2 for info in infos):
                    return {'ok': False, 'error': 'Манифест архива слишком большой.'}
                if deep:
                    first_bad = archive.testzip()
                    if first_bad:
                        return {
                            "ok": False,
                            "profile_key": profile_root.name,
                            "name": backup.name,
                            "path": str(backup),
                            "error": f"Corrupted zip member: {first_bad}",
                        }
                for info in archive.infolist():
                    name = info.filename
                    kind = self.classify_auth_backup_member(
                        name,
                        profile_root.name,
                        include_global=include_global,
                    )
                    if kind == "manifest":
                        try:
                            manifest = json.loads(archive.read(info).decode("utf-8"))
                            if not isinstance(manifest, dict):
                                manifest = {}
                        except Exception:
                            manifest = {}
                        continue
                    if info.is_dir():
                        continue
                    if kind == "blocked":
                        blocked.append(name)
                        continue
                    if kind == "profile":
                        profile_entries += 1
                    elif kind == "global":
                        global_entries += 1
                    entries.append({"name": name, "kind": kind, "size": int(info.file_size or 0)})
        except zipfile.BadZipFile as exc:
            return {
                "ok": False,
                "profile_key": profile_root.name,
                "name": backup.name,
                "path": str(backup),
                "error": f"Invalid zip: {exc}",
            }

        manifest_profile = str(manifest.get("profile_key") or "")
        profile_match = not manifest_profile or manifest_profile == profile_root.name
        return {
            "ok": profile_match and bool(entries),
            "profile_key": profile_root.name,
            "name": backup.name,
            "path": str(backup),
            "size_bytes": backup.stat().st_size if backup.exists() else 0,
            "manifest": manifest,
            "manifest_profile": manifest_profile,
            "profile_match": profile_match,
            "entry_count": len(entries),
            "profile_entry_count": profile_entries,
            "global_entry_count": global_entries,
            "blocked_count": len(blocked),
            "blocked": blocked[:20],
            "entries": entries[:30],
        }

    def restore_auth_backup(
        self,
        profile_key: str | None,
        backup_name: str,
        *,
        include_global: bool = False,
    ) -> dict[str, Any]:
        adapter = self.adapter
        profile_root = adapter._bot_profile_root(profile_key)
        worker = adapter.get_worker_status()
        if worker.running:
            raise RuntimeError("Stop worker before restoring authorization backup.")

        backup = self.auth_backup_by_name(profile_root.name, backup_name)
        if backup is None:
            raise FileNotFoundError(f"Backup not found for selected profile: {Path(str(backup_name or '')).name}")

        validation = self.validate_auth_backup(profile_root.name, backup.name, include_global=include_global, deep=True)
        if not validation.get("ok"):
            raise RuntimeError(str(validation.get("error") or "Backup validation failed"))

        if include_global and adapter.get_telegram_bot_status().running:
            raise RuntimeError('Остановите Telegram-бота перед восстановлением общих настроек.')
        pre_restore = self.create_auth_backup(profile_root.name)
        if pre_restore.get('error_count'):
            raise RuntimeError('Не удалось полностью сохранить текущий профиль. Восстановление отменено.')
        restored: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        # Fully extract and CRC-check before touching the live profile. Then
        # replace each directory/file, retaining originals for rollback.
        with restore_staging(adapter.base_dir) as staging:
            replacements: dict[Path, Path] = {}
            with protected_zip(self.adapter.base_dir, backup) as archive:
                for info in archive.infolist():
                    name = info.filename
                    if info.is_dir() or name == 'BACKUP_MANIFEST.json':
                        continue
                    kind = self.classify_auth_backup_member(name, profile_root.name, include_global=include_global)
                    if kind == 'blocked':
                        skipped.append({'name': name, 'reason': 'not_allowed'})
                        continue
                    relative = PurePosixPath(name)
                    destination = adapter.base_dir / relative
                    if destination.resolve() != destination.absolute():
                        raise ValueError('Путь восстановления содержит ссылку или перенаправление.')
                    if kind == 'profile':
                        components = relative.parts
                        length = 4 if components[2] == 'browser' else 3
                        root_rel = Path(*components[:length])
                    else:
                        root_rel = Path(*relative.parts)
                    live_root = adapter.base_dir / root_rel
                    staged_root = staging / 'new' / root_rel
                    if live_root not in replacements:
                        replacements[live_root] = staged_root
                        if kind == 'profile' and relative.parts[2] in {'logs', 'artifacts'} and live_root.exists():
                            shutil.copytree(live_root, staged_root)
                    target = staging / 'new' / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    restored.append({'name': name, 'kind': kind, 'bytes': info.file_size})
            from contextlib import ExitStack

            from yara_app.fileio import file_lock
            swapped = []
            with ExitStack() as locks:
                for live in sorted(replacements, key=str):
                    locks.enter_context(file_lock(live))
                try:
                    for index, (live, staged) in enumerate(replacements.items()):
                        previous = staging / f'previous_{index}'
                        live.parent.mkdir(parents=True, exist_ok=True)
                        existed = live.exists()
                        if existed:
                            live.rename(previous)
                        swapped.append((live, previous, existed))
                        staged.rename(live)
                except Exception:
                    for index, (live, previous, existed) in enumerate(reversed(swapped)):
                        if live.exists():
                            live.rename(staging / f'failed_{index}')
                        if existed:
                            previous.rename(live)
                    raise

        return {
            "ok": True,
            "profile_key": profile_root.name,
            "name": backup.name,
            "restored_count": len(restored),
            "skipped_count": len(skipped),
            "restored": restored[:40],
            "skipped": skipped[:40],
            "pre_restore_backup": pre_restore,
            "validation": validation,
            "browser_profile": adapter.browser_profile_summary(profile_root.name),
        }

    def prune_auth_backups(
        self,
        profile_key: str | None = None,
        *,
        keep_latest: int = 1,
    ) -> dict[str, Any]:
        adapter = self.adapter
        profile_root = adapter._bot_profile_root(profile_key)
        keep_latest = max(0, int(keep_latest))
        backups = sorted(
            self.auth_backup_files(profile_root.name),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        kept = backups[:keep_latest]
        removable = backups[keep_latest:]
        removed: list[dict[str, Any]] = []
        freed_bytes = 0
        for backup in removable:
            try:
                size = backup.stat().st_size
                backup.unlink()
            except OSError as exc:
                removed.append({"name": backup.name, "path": str(backup), "ok": False, "error": str(exc)})
                continue
            freed_bytes += size
            removed.append({"name": backup.name, "path": str(backup), "ok": True, "bytes": size})
        return {
            "profile_key": profile_root.name,
            "keep_latest": keep_latest,
            "kept": [{"name": path.name, "path": str(path)} for path in kept if path.exists()],
            "removed_count": sum(1 for item in removed if item.get("ok")),
            "removed": removed,
            "freed_bytes": freed_bytes,
            "backup_bytes": sum(path.stat().st_size for path in self.auth_backup_files(profile_root.name) if path.exists()),
        }

    def delete_selected_backups(self, profile_key: str | None, selections: list[dict[str, Any]]) -> dict[str, Any]:
        adapter = self.adapter
        profile_root = adapter._bot_profile_root(profile_key)
        browser_dir = profile_root / "browser"
        browser_backups = {path.name: path for path in adapter._browser_backup_dirs(browser_dir)}
        auth_backups = {path.name: path for path in self.auth_backup_files(profile_root.name)}
        removed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        freed_bytes = 0

        for item in selections:
            kind = str(item.get("kind") or "").strip().lower()
            name = Path(str(item.get("name") or "")).name
            if not name:
                skipped.append({"kind": kind, "name": name, "reason": "empty_name"})
                continue
            if kind == "auth":
                target = auth_backups.get(name)
            elif kind == "browser":
                target = browser_backups.get(name)
            else:
                target = None
            if target is None:
                skipped.append({"kind": kind, "name": name, "reason": "not_found_or_not_allowed"})
                continue

            try:
                size = target.stat().st_size if target.is_file() else adapter._dir_size_bytes(target)
                if target.is_dir():
                    adapter._remove_profile_path_for_compact(target, browser_dir)
                else:
                    target.unlink()
            except OSError as exc:
                skipped.append({"kind": kind, "name": name, "reason": str(exc)})
                continue

            if not target.exists():
                freed_bytes += size
                removed.append({"kind": kind, "name": name, "path": str(target), "bytes": size})
            else:
                skipped.append({"kind": kind, "name": name, "reason": "delete_failed"})

        return {
            "profile_key": profile_root.name,
            "removed_count": len(removed),
            "skipped_count": len(skipped),
            "removed": removed,
            "skipped": skipped,
            "freed_bytes": freed_bytes,
            "browser_profile": adapter.browser_profile_summary(profile_root.name),
        }

    def prune_browser_backups(
        self,
        profile_key: str | None = None,
        *,
        keep_latest: int = 1,
    ) -> dict[str, Any]:
        adapter = self.adapter
        profile_root = adapter._bot_profile_root(profile_key)
        browser_dir = profile_root / "browser"
        keep_latest = max(0, int(keep_latest))
        backups = sorted(
            adapter._browser_backup_dirs(browser_dir),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        kept = backups[:keep_latest]
        removable = backups[keep_latest:]

        removed: list[dict[str, Any]] = []
        freed_bytes = 0
        for backup in removable:
            size = adapter._dir_size_bytes(backup)
            adapter._remove_profile_path_for_compact(backup, browser_dir)
            if not backup.exists():
                freed_bytes += size
                removed.append({"name": backup.name, "path": str(backup), "bytes": size})

        return {
            "profile_key": profile_root.name,
            "browser_dir": str(browser_dir),
            "keep_latest": keep_latest,
            "kept": [{"name": path.name, "path": str(path)} for path in kept if path.exists()],
            "removed_count": len(removed),
            "removed": removed,
            "freed_bytes": freed_bytes,
            "backup_bytes": sum(adapter._dir_size_bytes(path) for path in adapter._browser_backup_dirs(browser_dir)),
            "size_bytes": adapter._dir_size_bytes(browser_dir / "user_data"),
        }

    def create_public_project_backup(self) -> dict[str, Any]:
        adapter = self.adapter
        backup_dir = adapter.base_dir / "backups" / "project"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"tiktok_heart_public_source_{stamp}.zip"
        counter = 1
        while target.exists():
            target = backup_dir / f"tiktok_heart_public_source_{stamp}_{counter}.zip"
            counter += 1

        root_files = {
            ".env.example",
            ".gitattributes",
            ".gitignore",
            "BUILD_INFO.json",
            "LICENSE",
            "README.md",
            "message_pool.txt",
            "package.json",
            "package-lock.json",
            "pyproject.toml",
            "pytest.ini",
            "requirements.txt",
            "requirements-desktop.txt",
            "requirements-dev.txt",
            "start_app.bat",
            "start_app.vbs",
        }
        included_roots = {"app_shell", "assets", "control", "docs", "scripts", "src-tauri", "tests", "yara_app"}
        blocked_roots = {
            ".git",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "backups",
            "logs",
            "node_modules",
            "profiles",
            "release",
            "src-tauri/target",
            "src-tauri/gen",
        }
        blocked_names = {"__pycache__"}
        blocked_suffixes = {
            ".pyc",
            ".pyo",
            ".pyd",
            ".log",
            ".tmp",
            ".bak",
            ".zip",
            ".7z",
            ".rar",
            ".msi",
            ".exe",
            ".db",
            ".sqlite",
            ".ldb",
            ".pma",
        }

        def is_blocked(path: Path) -> bool:
            rel = path.relative_to(adapter.base_dir).as_posix()
            parts = rel.split("/")
            if any(name in blocked_names for name in parts):
                return True
            return any(rel == root or rel.startswith(f"{root}/") for root in blocked_roots)

        def is_allowed(path: Path) -> bool:
            rel = path.relative_to(adapter.base_dir).as_posix()
            parts = rel.split("/")
            if is_blocked(path):
                return False
            if parts[0] == "control":
                return path.name.endswith(".example.json") or path.name == ".gitkeep"
            if path.name in root_files:
                return True
            if parts[0] not in included_roots:
                return False
            return path.suffix.lower() not in blocked_suffixes

        def iter_source_files() -> list[Path]:
            roots = [adapter.base_dir / name for name in sorted(root_files | included_roots)]
            files: list[Path] = []
            for root in roots:
                if not root.exists() or is_blocked(root):
                    continue
                if root.is_file():
                    if is_allowed(root):
                        files.append(root)
                    continue
                pending = [root]
                while pending:
                    current = pending.pop()
                    try:
                        children = list(current.iterdir())
                    except OSError:
                        continue
                    for child in children:
                        if is_blocked(child):
                            continue
                        try:
                            if child.is_dir():
                                pending.append(child)
                            elif child.is_file() and is_allowed(child):
                                files.append(child)
                        except OSError:
                            continue
            return files

        included_count = 0
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(iter_source_files()):
                archive.write(path, path.relative_to(adapter.base_dir).as_posix())
                included_count += 1

        return {
            "path": str(target),
            "name": target.name,
            "size_bytes": target.stat().st_size,
            "included_count": included_count,
            "excluded": sorted(blocked_roots),
        }

    def create_auth_backup(self, profile_key: str | None = None) -> dict[str, Any]:
        adapter = self.adapter
        profile_root = adapter._bot_profile_root(profile_key)
        backup_dir = adapter.base_dir / "backups" / "auth"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"auth_backup_{profile_root.name}_{stamp}.zip"
        counter = 1
        while target.exists():
            target = backup_dir / f"auth_backup_{profile_root.name}_{stamp}_{counter}.zip"
            counter += 1

        cache_dir_names = {
            "BrowserMetrics",
            "Cache",
            "Code Cache",
            "Crashpad",
            "DawnGraphiteCache",
            "DawnWebGPUCache",
            "GPUCache",
            "GrShaderCache",
            "Media Cache",
            "OptimizationGuidePredictionModels",
            "Safe Browsing",
            "ShaderCache",
            "component_crx_cache",
            "pnacl",
        }
        skip_suffixes = {".log", ".tmp", ".bak", ".pma"}
        roots = [
            profile_root / "browser" / "user_data",
            profile_root / "state",
            profile_root / "artifacts",
            profile_root / "logs",
            adapter.control_dir,
        ]
        root_files = [
            adapter.message_pool_path,
            adapter.base_dir / ".env",
            adapter.base_dir / ".env.local",
        ]
        errors: list[dict[str, str]] = []
        included_count = 0

        def should_include(path: Path) -> bool:
            try:
                parts = path.relative_to(profile_root / "browser" / "user_data").parts
            except ValueError:
                parts = ()
            if any(part in cache_dir_names for part in parts):
                return False
            return path.suffix.lower() not in skip_suffixes

        def add_file(archive: zipfile.ZipFile, path: Path) -> bool:
            if not path.exists() or not path.is_file() or not should_include(path):
                return False
            try:
                archive.write(path, path.relative_to(adapter.base_dir).as_posix())
                return True
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": str(path), "error": str(exc)})
                return False

        worker = adapter.get_worker_status()
        telegram = adapter.get_telegram_bot_status()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            manifest = {
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "profile_key": profile_root.name,
                "sensitive": True,
                "contains": [
                    "Chromium authorization profile without heavy caches",
                    "profile state/artifacts/logs",
                    "runtime control JSON",
                    "message pool",
                    ".env files when present",
                ],
                "worker_running": bool(worker.running),
                "telegram_running": bool(telegram.running),
                "note": "Keep this archive private. It can contain cookies, Telegram config, and local state.",
                "excluded_cache_dirs": sorted(cache_dir_names),
            }
            archive.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            included_count += 1
            browser = profile_root / 'browser'
            if vault_path(browser).exists() and not (browser / 'user_data/Default').exists():
                with protected_zip(adapter.base_dir, vault_path(browser), 'browser:' + profile_root.name) as source:
                    for info in source.infolist():
                        if info.is_dir() or not info.filename.startswith('user_data/'):
                            continue
                        if any(part in cache_dir_names for part in PurePosixPath(info.filename).parts):
                            continue
                        name = f'profiles/{profile_root.name}/browser/{info.filename}'
                        with source.open(info) as inp, archive.open(name, 'w') as out:
                            shutil.copyfileobj(inp, out, 1024 * 1024)
                        included_count += 1
            for root in roots:
                if not root.exists():
                    continue
                if root.is_file():
                    if add_file(archive, root):
                        included_count += 1
                    continue
                for path in sorted(root.rglob("*")):
                    if add_file(archive, path):
                        included_count += 1
            for path in root_files:
                if add_file(archive, path):
                    included_count += 1

        protect_backup(adapter.base_dir, target)
        return {
            "path": str(target),
            "name": target.name,
            "profile_key": profile_root.name,
            "size_bytes": target.stat().st_size,
            "included_count": included_count,
            "error_count": len(errors),
            "errors": errors[:20],
            "worker_running": bool(worker.running),
            "telegram_running": bool(telegram.running),
        }

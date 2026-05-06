from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = Path(__file__).resolve().parent / "web"
SERVER_STATE_FILE = PROJECT_ROOT / "control" / "app_shell_server.json"
PERF_LOG_FILE = PROJECT_ROOT / "logs" / "app_shell_perf.log"
SLOW_REQUEST_MS = int(os.getenv("APP_SHELL_SLOW_REQUEST_MS", "250"))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yara_app.project_adapter import ProjectAdapter  # noqa: E402


LOG_LABELS = {
    "worker_stdout": "Worker stdout",
    "log": "Worker app log",
    "auth_debug": "Auth debug",
    "launcher_log": "Launcher",
    "telegram_log": "Telegram bot",
    "app_shell_perf": "UI performance",
}
LOG_FILTERS = {"all", "errors", "warnings", "success", "important"}


def json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return json_ready(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    return value


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length") or "0"
    try:
        length = min(max(0, int(raw_length)), 1024 * 1024 * 2)
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    payload = handler.rfile.read(length)
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def find_free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 40)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free localhost port found.")


def write_server_state(port: int, url: str) -> None:
    try:
        SERVER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SERVER_STATE_FILE.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": port,
                    "url": url,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def append_perf_log(method: str, path: str, elapsed_ms: float) -> None:
    if elapsed_ms < SLOW_REQUEST_MS:
        return
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {method} {path} {elapsed_ms:.1f}ms\n"
    try:
        PERF_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PERF_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    print(f"[perf] {method} {path} {elapsed_ms:.1f}ms", flush=True)


def int_query(params: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((params.get(name) or [default])[0])
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_log_filter(value: str) -> str:
    mode = str(value or "all").strip().lower()
    aliases = {
        "error": "errors",
        "warning": "warnings",
        "successes": "success",
        "ok": "success",
        "critical": "important",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in LOG_FILTERS else "all"


def keep_log_line(line: str, *, mode: str, search: str) -> bool:
    low = line.lower()
    if mode == "errors" and not ("error" in low or "[error]" in low or "ошибка" in low or "traceback" in low):
        return False
    if mode == "warnings" and not ("warning" in low or "[warning]" in low or "предупрежд" in low):
        return False
    if mode == "success" and not ("success" in low or "успех" in low or "✅" in line):
        return False
    if mode == "important" and not any(
        token in low
        for token in ("error", "warning", "success", "успех", "auth", "login", "captcha", "409", "ошибка")
    ) and "✅" not in line:
        return False
    if search and search not in low:
        return False
    return True


def classify_log_line(line: str) -> str:
    low = line.lower()
    if any(token in low for token in ("[error]", "[fatal]", "traceback", "exception", "ошибка")):
        return "error"
    if "[warning]" in low or "предупрежд" in low or "warning" in low:
        return "warning"
    if "✅" in line or "[success]" in low or "успех" in low or "success" in low:
        return "success"
    if "[debug]" in low:
        return "debug"
    if "[info]" in low:
        return "info"
    return ""


class AppShellHandler(BaseHTTPRequestHandler):
    adapter = ProjectAdapter(PROJECT_ROOT)
    _diagnostics_cache: dict[str, Any] | None = None
    _diagnostics_cache_at = 0.0
    _diagnostics_cache_ttl = 5.0

    server_version = "TikTokHeartDesktop/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(json_ready(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, exc: Exception, status: int = 500) -> None:
        self._send_json({"ok": False, "error": str(exc), "type": type(exc).__name__}, status)

    @classmethod
    def _clear_diagnostics_cache(cls) -> None:
        cls._diagnostics_cache = None
        cls._diagnostics_cache_at = 0.0

    def _get_diagnostics(self, *, fresh: bool = False) -> Any:
        now = time.time()
        cached = self.__class__._diagnostics_cache
        cache_age = now - self.__class__._diagnostics_cache_at
        if not fresh and cached is not None and cache_age < self.__class__._diagnostics_cache_ttl:
            return cached
        data = self.adapter.diagnostics()
        self.__class__._diagnostics_cache = data
        self.__class__._diagnostics_cache_at = now
        return data

    def _send_static(self, request_path: str) -> None:
        rel = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        target = (WEB_ROOT / rel).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.exists() or not target.is_file():
            target = WEB_ROOT / "index.html"
        suffix = target.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        started = time.perf_counter()
        try:
            try:
                if parsed.path == "/api/diagnostics":
                    query = parse_qs(parsed.query)
                    self._send_json({"ok": True, "data": self._get_diagnostics(fresh=query.get("fresh") == ["1"])})
                    return
                if parsed.path == "/api/chrome-profiles":
                    self._send_json({"ok": True, "data": self.adapter.list_google_chrome_profiles()})
                    return
                if parsed.path == "/api/browser-profile":
                    query = parse_qs(parsed.query)
                    profile_key = (query.get("profile_key") or [""])[0].strip() or None
                    self._send_json({"ok": True, "data": self.adapter.browser_profile_summary(profile_key)})
                    return
                if parsed.path == "/api/profiles":
                    self._send_json({"ok": True, "data": self.adapter.get_profiles()})
                    return
                if parsed.path == "/api/schedule":
                    self._send_json({"ok": True, "data": self.adapter.get_worker_schedule_status()})
                    return
                if parsed.path == "/api/message-pool":
                    self._send_json({
                        "ok": True,
                        "data": {
                            "text": self.adapter.get_message_pool_text(),
                            "stats": self.adapter.get_message_pool_stats(),
                        },
                    })
                    return
                if parsed.path == "/api/logs":
                    self._handle_get_logs(parsed.query)
                    return
                if parsed.path == "/api/diagnostics-text":
                    diag = self._get_diagnostics(fresh=True)
                    self._send_json({"ok": True, "data": {"text": self._build_diagnostics_text(diag)}})
                    return
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(exc)
                return
            self._send_static(parsed.path)
        finally:
            append_perf_log("GET", parsed.path, (time.perf_counter() - started) * 1000)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        started = time.perf_counter()
        try:
            try:
                payload = read_json_body(self)
                self._clear_diagnostics_cache()
                if parsed.path == "/api/action":
                    self._handle_action(payload)
                    return
                if parsed.path == "/api/schedule":
                    self._handle_schedule(payload)
                    return
                if parsed.path == "/api/message-pool":
                    self._handle_message_pool(payload)
                    return
                if parsed.path == "/api/message-pool/backup":
                    self._handle_message_pool_backup(payload)
                    return
                if parsed.path == "/api/profile-action":
                    self._handle_profile_action(payload)
                    return
                if parsed.path == "/api/target-action":
                    self._handle_target_action(payload)
                    return
                if parsed.path == "/api/open-path":
                    self._handle_open_path(payload)
                    return
                if parsed.path == "/api/export-diagnostics":
                    self._handle_export_diagnostics(payload)
                    return
                if parsed.path == "/api/project-backup":
                    self._handle_project_backup(payload)
                    return
                if parsed.path == "/api/auth-backup":
                    self._handle_auth_backup(payload)
                    return
                if parsed.path == "/api/import-chrome":
                    self._handle_import_chrome(payload)
                    return
                if parsed.path == "/api/compact-browser":
                    self._handle_compact_browser(payload)
                    return
                if parsed.path == "/api/prune-browser-backups":
                    self._handle_prune_browser_backups(payload)
                    return
                if parsed.path == "/api/delete-backups":
                    self._handle_delete_backups(payload)
                    return
                if parsed.path == "/api/maintenance":
                    self._handle_maintenance(payload)
                    return
                if parsed.path == "/api/worker-self-test":
                    self._handle_worker_self_test(payload)
                    return
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(exc)
                return
            self._send_error_json(RuntimeError("Unknown endpoint"), HTTPStatus.NOT_FOUND)
        finally:
            append_perf_log("POST", parsed.path, (time.perf_counter() - started) * 1000)

    def _handle_action(self, payload: dict[str, Any]) -> None:
        action = str(payload.get("action") or "").strip()
        if action == "start_all":
            result = self.adapter.start_all()
        elif action == "stop_all":
            result = self.adapter.stop_all()
        elif action == "restart_all":
            result = self.adapter.restart_all()
        elif action == "start_worker":
            result = self.adapter.start_worker()
        elif action == "stop_worker":
            result = self.adapter.stop_worker()
        elif action == "restart_worker":
            result = self.adapter.restart_worker()
        elif action == "start_telegram":
            result = self.adapter.start_telegram_bot()
        elif action == "stop_telegram":
            result = self.adapter.stop_telegram_bot()
        elif action == "clear_telegram_lock":
            result = self.adapter.clear_telegram_lock()
        elif action == "reset_runtime_flags":
            result = self.adapter.reset_runtime_flags()
        elif action == "toggle_pause":
            state = self.adapter.get_control_state()
            result = self.adapter.set_paused(not bool(state.get("paused")))
        elif action == "toggle_dry_run":
            state = self.adapter.get_control_state()
            result = self.adapter.set_dry_run(not bool(state.get("dry_run")))
        else:
            raise ValueError(f"Unknown action: {action}")
        self._send_json({"ok": True, "data": result})

    def _handle_get_logs(self, raw_query: str) -> None:
        params = parse_qs(raw_query)
        name = str((params.get("name") or ["worker_stdout"])[0]).strip()
        lines = int_query(params, "lines", 160, 40, 900)
        mode = normalize_log_filter(str((params.get("filter") or ["all"])[0]))
        search = str((params.get("search") or [""])[0]).strip().lower()
        log_files = self.adapter.log_files()
        if name not in log_files:
            name = "worker_stdout"
        path = log_files[name]
        raw_lines = self.adapter.tail_file(path, lines=lines)
        filtered = [
            {"text": line, "kind": classify_log_line(line)}
            for line in raw_lines
            if keep_log_line(line, mode=mode, search=search)
        ]
        self._send_json({
            "ok": True,
            "data": {
                "name": name,
                "label": LOG_LABELS.get(name, name),
                "path": str(path),
                "exists": path.exists(),
                "line_limit": lines,
                "filter": mode,
                "search": search,
                "shown": len(filtered),
                "total": len(raw_lines),
                "options": [{"value": key, "label": label} for key, label in LOG_LABELS.items()],
                "filters": sorted(LOG_FILTERS),
                "lines": filtered,
            },
        })

    def _build_diagnostics_text(self, diag: dict[str, Any]) -> str:
        health = dict(diag.get("health") or {})
        browser = dict(diag.get("browser_profile") or {})
        schedule = dict(diag.get("worker_schedule") or {})
        run = dict(diag.get("run") or {})
        lines = [
            "TikTok Heart diagnostics",
            "",
            "Summary",
            f"Health: {health.get('score', 0)}/100 · {health.get('summary') or '—'}",
            f"Worker: {'running' if dict(diag.get('worker') or {}).get('running') else 'stopped'}",
            f"Telegram: {'running' if dict(diag.get('telegram_bot') or {}).get('running') else 'stopped'}",
            f"Active profile: {dict(diag.get('state') or {}).get('active_profile') or '—'}",
            f"Messages: {dict(diag.get('message_pool_details') or {}).get('unique_count', 0)} unique",
            "",
            "Browser",
            f"user_data: {browser.get('user_data_dir') or '—'}",
            f"cookies: {'yes' if browser.get('cookies_exists') else 'no'}",
            f"needs recovery: {'yes' if browser.get('needs_recovery') else 'no'}",
            f"size: {browser.get('size_bytes', 0)} bytes",
            "",
            "Schedule",
            f"installed: {'yes' if schedule.get('installed') else 'no'}",
            f"triggers: logon={bool(schedule.get('at_logon'))}, 12h={bool(schedule.get('every_12_hours'))}",
            f"next run: {schedule.get('next_run_time') or '—'}",
            "",
            "Current run",
            json.dumps(run, ensure_ascii=False, indent=2),
            "",
            "Issues",
        ]
        issues = list(health.get("issues") or [])
        if issues:
            lines.extend(f"- [{item.get('severity')}] {item.get('title')}: {item.get('details')}" for item in issues)
        else:
            lines.append("- none")
        lines.extend(["", "Raw diagnostics", json.dumps(diag, ensure_ascii=False, indent=2)])
        return "\n".join(lines).strip() + "\n"

    def _handle_schedule(self, payload: dict[str, Any]) -> None:
        enabled = bool(payload.get("enabled", True))
        if not enabled:
            result = self.adapter.unregister_worker_schedule()
        else:
            result = self.adapter.register_worker_schedule(
                at_logon=bool(payload.get("at_logon", True)),
                every_12_hours=bool(payload.get("every_12_hours", True)),
            )
        self._send_json({"ok": True, "data": result})

    def _handle_message_pool(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        if bool(payload.get("backup", True)):
            self.adapter.create_message_pool_backup()
        if bool(payload.get("normalize", True)):
            result = self.adapter.save_message_pool_text(text)
        else:
            result = self.adapter.save_message_pool_text_raw(text)
        self._send_json({"ok": True, "data": result})

    def _handle_message_pool_backup(self, payload: dict[str, Any]) -> None:
        text = payload.get("text")
        backup = self.adapter.create_message_pool_backup(str(text) if text is not None else None)
        self._send_json({"ok": True, "data": {"path": str(backup), "name": backup.name}})

    def _handle_profile_action(self, payload: dict[str, Any]) -> None:
        action = str(payload.get("action") or "").strip()
        index = int(payload.get("index") or 0)
        if action == "set_active":
            result = self.adapter.set_active_profile(index)
        elif action == "toggle":
            result = self.adapter.toggle_profile(index)
        else:
            raise ValueError(f"Unknown profile action: {action}")
        self._send_json({"ok": True, "data": result})

    def _handle_target_action(self, payload: dict[str, Any]) -> None:
        action = str(payload.get("action") or "").strip()
        profile_key = str(payload.get("profile_key") or "") or None
        target_name = str(payload.get("target_name") or "").strip()
        if not target_name:
            raise ValueError("target_name is required")
        if action == "reset_cooldown":
            removed, path = self.adapter.reset_target_cooldown(target_name, profile_key)
            result = {"removed": removed, "path": path}
        elif action == "set_streak":
            value = int(payload.get("value") or 0)
            written, path = self.adapter.set_target_streak(target_name, value, profile_key)
            result = {"written": written, "path": path, "value": value}
        else:
            raise ValueError(f"Unknown target action: {action}")
        self._send_json({"ok": True, "data": result})

    def _resolve_project_path(self, payload: dict[str, Any]) -> Path:
        kind = str(payload.get("kind") or "").strip()
        if kind == "project":
            return PROJECT_ROOT
        if kind == "logs":
            return self.adapter.get_active_profile_logs_dir()
        if kind == "common_logs":
            return self.adapter.get_common_logs_dir()
        if kind == "backups":
            target = self.adapter.base_dir / "backups"
            target.mkdir(parents=True, exist_ok=True)
            return target
        if kind == "browser_profile":
            active = str(self.adapter.get_control_state().get("active_profile") or "") or None
            return Path(str(self.adapter.browser_profile_summary(active).get("user_data_dir") or PROJECT_ROOT))

        raw = str(payload.get("path") or "").strip()
        if not raw:
            raise ValueError("path is required")
        target = Path(raw)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target = target.resolve()
        allowed_roots = [PROJECT_ROOT.resolve()]
        try:
            for root in allowed_roots:
                target.relative_to(root)
                return target
        except ValueError as exc:
            raise ValueError(f"Refusing to open path outside project: {target}") from exc
        return target

    def _handle_open_path(self, payload: dict[str, Any]) -> None:
        target = self._resolve_project_path(payload)
        if not target.exists():
            raise FileNotFoundError(str(target))
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            import subprocess

            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(target)])
        self._send_json({"ok": True, "data": {"path": str(target)}})

    def _handle_export_diagnostics(self, payload: dict[str, Any]) -> None:
        fmt = str(payload.get("format") or "json").strip().lower()
        if fmt not in {"json", "txt"}:
            raise ValueError("format must be json or txt")
        diag = self.adapter.diagnostics()
        target_dir = self.adapter.get_common_logs_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if fmt == "json":
            target = target_dir / f"diagnostics_export_{stamp}.json"
            target.write_text(json.dumps(json_ready(diag), ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            target = target_dir / f"diagnostics_export_{stamp}.txt"
            target.write_text(self._build_diagnostics_text(diag), encoding="utf-8")
        self._send_json({"ok": True, "data": {"path": str(target), "name": target.name}})

    def _handle_project_backup(self, _payload: dict[str, Any]) -> None:
        result = self.adapter.create_public_project_backup()
        self._send_json({"ok": True, "data": result})

    def _handle_auth_backup(self, payload: dict[str, Any]) -> None:
        result = self.adapter.create_auth_backup(
            profile_key=str(payload.get("bot_profile_key") or "") or None,
        )
        self._send_json({"ok": True, "data": result})

    def _handle_import_chrome(self, payload: dict[str, Any]) -> None:
        result = self.adapter.import_google_chrome_profile(
            chrome_profile_id=str(payload.get("chrome_profile_id") or "Default"),
            bot_profile_key=str(payload.get("bot_profile_key") or "") or None,
            copy_mode=str(payload.get("copy_mode") or "tiktok_session"),
        )
        self._send_json({"ok": True, "data": result})

    def _handle_compact_browser(self, payload: dict[str, Any]) -> None:
        result = self.adapter.compact_browser_profile(
            profile_key=str(payload.get("bot_profile_key") or "") or None,
            filter_cookies=bool(payload.get("filter_cookies", True)),
        )
        self._send_json({"ok": True, "data": result})

    def _handle_prune_browser_backups(self, payload: dict[str, Any]) -> None:
        profile_key = str(payload.get("bot_profile_key") or "") or None
        keep_latest = int(payload.get("keep_latest") or 1)
        browser_result = self.adapter.prune_browser_backups(profile_key=profile_key, keep_latest=keep_latest)
        auth_result = self.adapter.prune_auth_backups(profile_key=profile_key, keep_latest=keep_latest)
        result = {
            **browser_result,
            "removed_count": int(browser_result.get("removed_count") or 0) + int(auth_result.get("removed_count") or 0),
            "freed_bytes": int(browser_result.get("freed_bytes") or 0) + int(auth_result.get("freed_bytes") or 0),
            "browser_backups": browser_result,
            "auth_backups": auth_result,
        }
        self._send_json({"ok": True, "data": result})

    def _handle_delete_backups(self, payload: dict[str, Any]) -> None:
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list):
            raise ValueError("items must be a list")
        items = [item for item in raw_items if isinstance(item, dict)]
        result = self.adapter.delete_selected_backups(
            profile_key=str(payload.get("bot_profile_key") or "") or None,
            selections=items,
        )
        self._send_json({"ok": True, "data": result})

    def _handle_maintenance(self, payload: dict[str, Any]) -> None:
        result = self.adapter.run_maintenance(
            profile_key=str(payload.get("bot_profile_key") or "") or None,
        )
        self._send_json({"ok": True, "data": result})

    def _handle_worker_self_test(self, _payload: dict[str, Any]) -> None:
        result = self.adapter.worker_starter_self_test()
        self._send_json({"ok": True, "data": result})


def main() -> int:
    parser = argparse.ArgumentParser(description="Modern local web shell for TikTok Heart desktop.")
    parser.add_argument("--port", type=int, default=5874)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()

    port = find_free_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), AppShellHandler)
    url = f"http://127.0.0.1:{port}/"
    write_server_state(port, url)
    print(f"TikTok Heart Desktop: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    if args.open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yara_app.notifications import notify_worker_result  # noqa: E402
from yara_app.project_adapter import ProjectAdapter  # noqa: E402

DEFAULT_COOLDOWN_GRACE_SECONDS = 180
DEFAULT_BOOT_FORCE_WINDOW_SECONDS = 900
BOOT_FORCE_MARKER_NAME = "boot_force_startup.json"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def print_lifecycle(event: str, **data) -> None:
    payload = " ".join(f"{key}={value}" for key, value in data.items() if value is not None)
    print(f"[starter] {event} {payload}".rstrip(), file=sys.stderr)


def cooldown_grace_seconds() -> int:
    raw = os.getenv("WORKER_COOLDOWN_GRACE_SECONDS", str(DEFAULT_COOLDOWN_GRACE_SECONDS))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_COOLDOWN_GRACE_SECONDS


def boot_force_window_seconds() -> int:
    raw = os.getenv("WORKER_BOOT_FORCE_WINDOW_SECONDS", str(DEFAULT_BOOT_FORCE_WINDOW_SECONDS))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_BOOT_FORCE_WINDOW_SECONDS


def system_uptime_seconds() -> int | None:
    if os.name == "nt":
        try:
            import ctypes

            return int(ctypes.windll.kernel32.GetTickCount64() // 1000)  # type: ignore[attr-defined]
        except Exception:
            return None
    try:
        import psutil  # type: ignore

        return int(time.time() - float(psutil.boot_time()))
    except Exception:
        return None


def boot_session_id(uptime_seconds: int) -> str:
    boot_started_at = max(0, int(time.time() - max(0, uptime_seconds)))
    return str(boot_started_at // 60)


def boot_force_marker_path(adapter: ProjectAdapter) -> Path:
    return adapter._get_profile_state_dir() / BOOT_FORCE_MARKER_NAME


def boot_force_already_used(adapter: ProjectAdapter, session_id: str) -> bool:
    path = boot_force_marker_path(adapter)
    try:
        marker = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    return str(marker.get("boot_session_id") or "") == session_id


def mark_boot_force_used(adapter: ProjectAdapter, session_id: str, uptime_seconds: int) -> None:
    path = boot_force_marker_path(adapter)
    payload = {
        "boot_session_id": session_id,
        "used_at": time.time(),
        "uptime_s": uptime_seconds,
        "pid": os.getpid(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print_lifecycle("boot_force_marker_write_failed", error=exc)


def configure_boot_force_environment(adapter: ProjectAdapter | None = None, *, force: bool = False) -> bool:
    window = boot_force_window_seconds()
    if window <= 0 and not force:
        return False
    uptime = system_uptime_seconds()
    if uptime is None:
        if not force:
            return False
        uptime = 0
    if not force and uptime > window:
        return False
    session_id = boot_session_id(uptime)
    if adapter is not None:
        if boot_force_already_used(adapter, session_id):
            print_lifecycle("boot_force_skipped", reason="already_used", uptime_s=uptime, window_s=window)
            return False
        mark_boot_force_used(adapter, session_id, uptime)
    os.environ["WORKER_FORCE_SEND_ON_BOOT"] = "1"
    print_lifecycle(
        "boot_force_enabled",
        uptime_s=uptime,
        window_s=window,
        session=session_id,
        reason="explicit_startup_task" if force else "recent_boot",
    )
    return True


def wait_for_near_cooldown(adapter: ProjectAdapter, queue: dict, *, sleep=time.sleep) -> tuple[dict, bool]:
    if int(queue.get("target_count") or 0) <= 0 or int(queue.get("ready_count") or 0) > 0:
        return queue, False
    wait_seconds = int(queue.get("next_send_in_s") or 0)
    grace_seconds = cooldown_grace_seconds()
    if wait_seconds > grace_seconds:
        return queue, False
    delay = max(2, wait_seconds + 2)
    print_lifecycle(
        "wait_near_cooldown",
        target_count=queue.get("target_count"),
        next_target=queue.get("next_target"),
        next_ready_in_s=wait_seconds,
        delay_s=delay,
        grace_s=grace_seconds,
    )
    sleep(delay)
    refreshed = adapter.queue_snapshot()
    print_lifecycle(
        "cooldown_recheck",
        ready_count=refreshed.get("ready_count"),
        cooldown_count=refreshed.get("cooldown_count"),
        next_ready_in_s=refreshed.get("next_send_in_s"),
    )
    return refreshed, True


def run_self_test(adapter: ProjectAdapter, *, browser: bool = True) -> dict:
    adapter.ensure_runtime_files()
    validation = adapter.validate_project()
    dependencies = adapter.dependency_report()
    preflight = adapter.runtime_preflight(dependencies)
    status = adapter.get_worker_status()
    queue = adapter.queue_snapshot()
    messages = adapter.get_message_pool_stats()
    browser_probe = {"enabled": False, "ok": True}
    if browser:
        os.environ["TG_DISABLE_NOTIFICATIONS"] = "1"
        os.environ.setdefault("WORK_BROWSER_HEADLESS", "1")
        with redirect_stdout(sys.stderr):
            from yara_app import tiktok_checker

            browser_probe = {"enabled": True, **tiktok_checker.run_browser_self_test()}
    return {
        "ok": bool(validation.get("ok")) and bool(preflight.get("ok")) and adapter.main_script_path.exists(),
        "safe_mode": "no_send",
        "project_root": str(adapter.base_dir),
        "main_script": str(adapter.main_script_path),
        "main_script_exists": adapter.main_script_path.exists(),
        "control_state": str(adapter.control_state_path),
        "profiles": str(adapter.profiles_path),
        "message_pool": str(adapter.message_pool_path),
        "message_count": messages.get("count") or messages.get("unique_count") or 0,
        "queue": queue,
        "browser_probe": browser_probe,
        "python": dependencies.get("python", {}),
        "validation": validation,
        "preflight": preflight,
        "worker_running": status.running,
        "worker_pid": status.pid,
    }


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Start the Yara worker once.")
    parser.add_argument("--self-test", action="store_true", help="validate runtime without starting worker")
    parser.add_argument("--no-browser", action="store_true", help="skip Chromium auth probe during self-test")
    parser.add_argument("--boot-force", action="store_true", help="force one cooldown bypass for a Windows startup task")
    args = parser.parse_args()

    adapter = ProjectAdapter(PROJECT_ROOT)
    adapter.ensure_runtime_files()
    print_lifecycle("start", self_test=args.self_test, no_browser=args.no_browser)

    if args.self_test:
        result = run_self_test(adapter, browser=not args.no_browser)
        if result.get("browser_probe", {}).get("enabled"):
            result["ok"] = bool(result.get("ok")) and bool(result["browser_probe"].get("ok"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print_lifecycle("self_test_finished", ok=result.get("ok"))
        return 0 if result.get("ok") else 2

    status = adapter.get_worker_status()
    state = adapter.get_control_state()
    def resume_queue():
        resume = getattr(adapter, 'resume_worker_queue', None)
        if resume:
            resume()
    running_profile = adapter._running_worker_profile(status) if status.running else None
    if status.running and running_profile in (None, state.get('active_profile')):
        resume_queue()
        print_lifecycle("skip_worker_running", pid=status.pid)
        return 0
    def report_blocked(reason: str) -> None:
        notify_worker_result(adapter, {'profile_name': state.get('active_profile'), 'results': []},
                             status='failed', reason=reason)
        resume_queue()

    if state.get('paused') or state.get('stop_requested'):
        print_lifecycle('skip_control_flag', paused=bool(state.get('paused')), stop_requested=bool(state.get('stop_requested')))
        return 0
    active = next((profile for profile in adapter.get_profiles() if profile.key == state.get('active_profile')), None)
    if active is not None and not active.enabled:
        resume_queue()
        print_lifecycle('skip_profile_disabled')
        return 0
    profile_root = adapter._bot_profile_root()
    if (profile_root / 'state' / 'session_import_required.json').exists():
        print_lifecycle('skip_session_import_required')
        report_blocked('session_import_required')
        return 0
    if (profile_root / 'browser' / '.session_import.lock').exists():
        print_lifecycle('skip_session_import_running')
        report_blocked('session_import_running')
        return 0
    if (profile_root / 'browser' / '.manual_login.json').exists():
        print_lifecycle('skip_manual_login_pending')
        report_blocked('manual_login_pending')
        return 0
    backoff_left = adapter._auth_backoff_seconds_left(profile_root / 'state' / 'auth_backoff.json')
    if backoff_left > 0:
        print_lifecycle('skip_auth_backoff', seconds_left=backoff_left)
        report_blocked('auth_backoff')
        return 0
    queue = adapter.queue_snapshot()
    if int(queue.get('target_count') or 0) == 0:
        print_lifecycle('skip_no_targets')
        report_blocked('no_targets')
        return 0
    # Only the explicit sign-in task may bypass cooldown. A recovery tick after
    # a recent reboot must still honour the user's disabled startup setting.
    boot_force = configure_boot_force_environment(adapter, force=True) if args.boot_force else False
    if not boot_force:
        queue, _waited_for_cooldown = wait_for_near_cooldown(adapter, queue)
    if not boot_force and int(queue.get("target_count") or 0) > 0 and int(queue.get("ready_count") or 0) == 0:
        resume_queue()
        if int(queue.get('cooldown_count') or 0) < int(queue.get('target_count') or 0):
            report_blocked('delivery_unconfirmed')
        wait_seconds = int(queue.get("next_send_in_s") or 0)
        print_lifecycle("skip_all_cooldown", target_count=queue.get("target_count"), next_ready_in_s=wait_seconds)
        try:
            adjustment = adapter.scheduler.reanchor_from_queue()
            print_lifecycle('schedule_aligned', **adjustment)
        except Exception:
            print_lifecycle('schedule_update_failed', reason='check_task_permissions_and_save_schedule')
            return 3
        return 0

    try:
        started = adapter.start_worker()
    except Exception:
        report_blocked('worker_start_failed')
        raise
    print_lifecycle("worker_started", pid=started.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

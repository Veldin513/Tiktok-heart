from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yara_app.project_adapter import ProjectAdapter


def run_self_test(adapter: ProjectAdapter) -> dict:
    adapter.ensure_runtime_files()
    validation = adapter.validate_project()
    dependencies = adapter.dependency_report()
    preflight = adapter.runtime_preflight(dependencies)
    status = adapter.get_worker_status()
    return {
        "ok": bool(validation.get("ok")) and bool(preflight.get("ok")) and adapter.main_script_path.exists(),
        "project_root": str(adapter.base_dir),
        "main_script": str(adapter.main_script_path),
        "main_script_exists": adapter.main_script_path.exists(),
        "control_state": str(adapter.control_state_path),
        "profiles": str(adapter.profiles_path),
        "message_pool": str(adapter.message_pool_path),
        "python": dependencies.get("python", {}),
        "validation": validation,
        "preflight": preflight,
        "worker_running": status.running,
        "worker_pid": status.pid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the Yara worker once.")
    parser.add_argument("--self-test", action="store_true", help="validate runtime without starting worker")
    args = parser.parse_args()

    adapter = ProjectAdapter(PROJECT_ROOT)
    adapter.ensure_runtime_files()

    if args.self_test:
        result = run_self_test(adapter)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    status = adapter.get_worker_status()
    if status.running:
        print(f"Worker already running, PID {status.pid}")
        return 0

    started = adapter.start_worker()
    print(f"Worker started, PID {started.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

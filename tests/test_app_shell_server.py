from __future__ import annotations

import json
from pathlib import Path

import app_shell.server as server
from yara_app.project_adapter import ProjectAdapter


def test_write_server_state_records_runtime_url(tmp_path: Path, monkeypatch) -> None:
    state_file = tmp_path / "control" / "app_shell_server.json"
    monkeypatch.setattr(server, "SERVER_STATE_FILE", state_file)

    server.write_server_state(5999, "http://127.0.0.1:5999/")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["port"] == 5999
    assert data["url"] == "http://127.0.0.1:5999/"
    assert isinstance(data["pid"], int)


def test_append_perf_log_writes_only_slow_requests(tmp_path: Path, monkeypatch) -> None:
    perf_log = tmp_path / "logs" / "app_shell_perf.log"
    monkeypatch.setattr(server, "PERF_LOG_FILE", perf_log)
    monkeypatch.setattr(server, "SLOW_REQUEST_MS", 100)

    server.append_perf_log("GET", "/api/diagnostics", 99.9)
    assert not perf_log.exists()

    server.append_perf_log("GET", "/api/diagnostics", 101.0)
    assert "/api/diagnostics 101.0ms" in perf_log.read_text(encoding="utf-8")


def test_project_adapter_exposes_app_shell_perf_log(tmp_path: Path) -> None:
    paths = ProjectAdapter(tmp_path).log_files()

    assert paths["app_shell_perf"] == tmp_path / "logs" / "app_shell_perf.log"


def test_project_adapter_summarizes_app_shell_performance(tmp_path: Path) -> None:
    perf_log = tmp_path / "logs" / "app_shell_perf.log"
    perf_log.parent.mkdir(parents=True)
    perf_log.write_text(
        "\n".join(
            [
                "2026-05-06 13:00:00 GET /api/diagnostics 320.5ms",
                "2026-05-06 13:00:01 POST /api/security-scan 1234.0ms",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = ProjectAdapter(tmp_path).app_shell_performance_summary()

    assert summary["slow_count"] == 2
    assert summary["warning_count"] == 1
    assert summary["status"] == "warning"
    assert summary["latest"]["path"] == "/api/security-scan"
    assert summary["worst"]["elapsed_ms"] == 1234.0


def test_project_adapter_runs_security_scan_script(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "security_scan.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('scan ok')\n", encoding="utf-8")

    result = ProjectAdapter(tmp_path).run_security_scan()

    assert result["ok"] is True
    assert result["tracked_only"] is True
    assert result["exit_code"] == 0
    assert result["lines"] == ["scan ok"]

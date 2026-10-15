from __future__ import annotations

import io
import json
import socket
import threading
from pathlib import Path

import app_shell.server as server
from yara_app.project_adapter import ProjectAdapter


class FakeRequest:
    def __init__(self, payload: bytes, content_length: str | None = None) -> None:
        self.headers = {"Content-Length": content_length or str(len(payload))}
        self.rfile = io.BytesIO(payload)


def test_read_json_body_accepts_only_complete_json_objects() -> None:
    assert server.read_json_body(FakeRequest(b'{"action":"start"}')) == {"action": "start"}

    for payload in (b"[]", b"null", b'"text"', b"{"):
        try:
            server.read_json_body(FakeRequest(payload))
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for {payload!r}")


def test_read_json_body_rejects_invalid_lengths_without_reading_payload() -> None:
    oversized = FakeRequest(b"", str(server.MAX_JSON_BODY_BYTES + 1))
    for request in (FakeRequest(b"", "-1"), FakeRequest(b"", "not-a-number"), oversized):
        try:
            server.read_json_body(request)
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for {request.headers['Content-Length']!r}")

    try:
        server.read_json_body(FakeRequest(b"{}", "3"))
    except ValueError as exc:
        assert "Incomplete" in str(exc)
    else:
        raise AssertionError("Expected ValueError for a truncated body")


def test_json_responses_include_security_headers() -> None:
    handler = object.__new__(server.AppShellHandler)
    sent_headers: dict[str, str] = {}
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: sent_headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()

    handler._send_json({"ok": True})

    assert handler.wfile.getvalue() == b'{\n  "ok": true\n}'
    assert {name: sent_headers[name] for name in server.SECURITY_HEADERS} == server.SECURITY_HEADERS


def test_post_returns_bad_request_for_invalid_json_body(monkeypatch) -> None:
    handler = object.__new__(server.AppShellHandler)
    handler.path = "/api/action"
    handler.headers = {"Content-Length": "invalid"}
    handler.rfile = io.BytesIO()
    sent: list[tuple[Exception, int]] = []
    handler._send_error_json = lambda exc, status=500: sent.append((exc, int(status)))
    monkeypatch.setattr(server, "append_perf_log", lambda *args: None)

    handler.do_POST()

    assert len(sent) == 1
    assert isinstance(sent[0][0], ValueError)
    assert sent[0][1] == 400


def test_write_server_state_records_runtime_url(tmp_path: Path, monkeypatch) -> None:
    state_file = tmp_path / "control" / "app_shell_server.json"
    monkeypatch.setattr(server, "SERVER_STATE_FILE", state_file)

    server.write_server_state(5999, "http://127.0.0.1:5999/")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["port"] == 5999
    assert data["url"] == "http://127.0.0.1:5999/"
    assert isinstance(data["pid"], int)


def test_read_live_server_url_requires_open_local_port(tmp_path: Path, monkeypatch) -> None:
    state_file = tmp_path / "control" / "app_shell_server.json"
    monkeypatch.setattr(server, "SERVER_STATE_FILE", state_file)
    server.write_server_state(5999, "http://127.0.0.1:5999/")
    assert server.read_live_server_url() is None

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])
        server.write_server_state(port, f"http://127.0.0.1:{port}/")
        assert server.read_live_server_url() == f"http://127.0.0.1:{port}/"


def test_append_perf_log_writes_only_slow_requests(tmp_path: Path, monkeypatch, capsys) -> None:
    perf_log = tmp_path / "logs" / "app_shell_perf.log"
    monkeypatch.setattr(server, "PERF_LOG_FILE", perf_log)
    monkeypatch.setattr(server, "SLOW_REQUEST_MS", 100)
    monkeypatch.delenv("APP_SHELL_PERF_STDOUT", raising=False)

    server.append_perf_log("GET", "/api/diagnostics", 99.9)
    assert not perf_log.exists()

    server.append_perf_log("GET", "/api/diagnostics", 101.0)
    assert "/api/diagnostics 101.0ms" in perf_log.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("APP_SHELL_PERF_STDOUT", "1")
    server.append_perf_log("GET", "/api/live", 120.0)
    assert "[perf] GET /api/live 120.0ms" in capsys.readouterr().out


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
                "2026-05-06 13:00:01 GET /api/live 1234.0ms",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = ProjectAdapter(tmp_path).app_shell_performance_summary()

    assert summary["slow_count"] == 2
    assert summary["warning_count"] == 1
    assert summary["status"] == "warning"
    assert summary["latest"]["path"] == "/api/live"
    assert summary["worst"]["elapsed_ms"] == 1234.0


def test_project_adapter_summarizes_worker_phase_performance(tmp_path: Path) -> None:
    history = tmp_path / "profiles" / "main" / "artifacts" / "run_history.jsonl"
    history.parent.mkdir(parents=True)
    rows = [
        {"event": "phase_timing", "phase": "internet_check", "elapsed_ms": 120.0, "time": "2026-05-06 13:00:00"},
        {"event": "phase_timing", "phase": "target_send_message", "elapsed_ms": 6200.0, "time": "2026-05-06 13:00:01"},
    ]
    history.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary = ProjectAdapter(tmp_path).worker_phase_summary("main")

    assert summary["phase_count"] == 2
    assert summary["warning_count"] == 1
    assert summary["status"] == "warning"
    assert summary["worst"]["phase"] == "target_send_message"


def test_project_adapter_live_status_is_lightweight(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir()
    (control / "profiles.json").write_text(
        json.dumps(
            {
                "main": {
                    "name": "main",
                    "targets": [{"name": "target_one", "url": "https://example.test"}],
                }
            }
        ),
        encoding="utf-8",
    )
    (control / "control_state.json").write_text(
        json.dumps({"active_profile": "main", "cooldown_hours": 12}),
        encoding="utf-8",
    )

    live = ProjectAdapter(tmp_path).live_status()

    assert live["mode"] == "live"
    assert live["profiles"]["total"] == 1
    assert live["queue"]["target_count"] == 1
    assert "size_bytes" not in live["browser_profile"]
    assert "performance" not in live
    assert "run_history" not in live
    assert "worker_lifecycle" not in live


def test_schedule_handler_keeps_startup_exception(monkeypatch) -> None:
    class FakeAdapter:
        def __init__(self) -> None:
            self.register_payload = None

        def register_worker_schedule(self, **kwargs):
            self.register_payload = kwargs
            return {"installed": True, **kwargs}

        def unregister_worker_schedule(self):
            return {"installed": False}

        def get_control_state(self):
            return {"active_profile": "alpha"}

        def queue_snapshot(self, profile_key=None):
            return {
                "profile_key": profile_key,
                "target_count": 1,
                "ready_count": 0,
                "next_send_at_text": "2026-07-15 17:29:31",
            }

    adapter = FakeAdapter()
    handler = object.__new__(server.AppShellHandler)
    handler.adapter = adapter
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))
    monkeypatch.setattr(server.AppShellHandler, "_schedule_lock", threading.Lock())

    handler._handle_schedule(
        {
            "enabled": True,
            "at_startup": True,
            "at_logon": False,
            "every_12_hours": True,
        }
    )

    assert adapter.register_payload == {
        "at_startup": True,
        "at_logon": False,
        "every_12_hours": True,
        "first_run_at": "2026-07-15 17:29:31",
    }
    assert sent[0][0]["data"]["at_startup"] is True
    assert sent[0][0]["data"]["at_logon"] is False

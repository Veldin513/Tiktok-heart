from __future__ import annotations

import json
from pathlib import Path

from yara_app.project_adapter import ProjectAdapter
from yara_app.tiktok_checker import trim_run_history


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_get_run_history_reads_recent_jsonl_entries(tmp_path: Path) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": []})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha"})
    history = tmp_path / "profiles" / "alpha" / "artifacts" / "run_history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        "\n".join(
            [
                json.dumps({"event": "run_started", "time": "2026-05-02 01:00:00"}),
                "not-json",
                json.dumps({"event": "target_result", "target": "alice", "success": True}),
            ]
        ),
        encoding="utf-8",
    )

    items = ProjectAdapter(tmp_path).get_run_history(limit=5)

    assert [item["event"] for item in items] == ["run_started", "target_result"]
    assert items[1]["target"] == "alice"
    assert items[1]["profile_name"] == "alpha"


def test_current_run_snapshot_exposes_terminal_result_metadata(tmp_path: Path) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": []})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha"})
    artifacts = tmp_path / "profiles" / "alpha" / "artifacts"
    write_json(
        artifacts / "run_state.json",
        {
            "status": "success",
            "timestamp": 1000.0,
            "completed_at": 999.0,
            "reason": "",
            "total_targets": 1,
        },
    )
    write_json(
        artifacts / "run_summary.json",
        {
            "total_targets": 1,
            "success_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "duration_seconds": 12.5,
            "results": [],
        },
    )

    run = ProjectAdapter(tmp_path).get_current_run_snapshot()

    assert run["status"] == "success"
    assert run["updated_at"] == 999.0
    assert run["completed_at"] == 999.0
    assert run["is_active"] is False
    assert run["success_count"] == 1


def test_current_run_snapshot_upgrades_legacy_idle_summary(tmp_path: Path) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": []})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha"})
    artifacts = tmp_path / "profiles" / "alpha" / "artifacts"
    write_json(artifacts / "run_state.json", {"status": "idle", "timestamp": 1000.0})
    write_json(
        artifacts / "run_summary.json",
        {
            "total_targets": 2,
            "success_count": 1,
            "skipped_count": 0,
            "failed_count": 1,
            "results": [],
        },
    )

    run = ProjectAdapter(tmp_path).get_current_run_snapshot()

    assert run["status"] == "partial"


def test_trim_run_history_keeps_complete_recent_json_records(tmp_path: Path) -> None:
    history = tmp_path / "run_history.jsonl"
    rows = [
        json.dumps({"event": "phase_timing", "index": index, "detail": "x" * 80})
        for index in range(80)
    ]
    history.write_text("\n".join(rows) + "\n", encoding="utf-8")

    trim_run_history(history, max_bytes=1_000, keep_bytes=600)

    kept = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert history.stat().st_size < 1_000
    assert kept
    assert kept[-1]["index"] == 79


def test_get_run_history_reads_recent_tail_without_partial_json_record(tmp_path: Path) -> None:
    write_json(tmp_path / "control" / "profiles.json", {"alpha": []})
    write_json(tmp_path / "control" / "control_state.json", {"active_profile": "alpha"})
    history = tmp_path / "profiles" / "alpha" / "artifacts" / "run_history.jsonl"
    history.parent.mkdir(parents=True)
    rows = [
        json.dumps({"event": "phase_timing", "index": index, "detail": "x" * 2_000})
        for index in range(300)
    ]
    history.write_text("\n".join(rows) + "\n", encoding="utf-8")

    items = ProjectAdapter(tmp_path).get_run_history(limit=3)

    assert [item["index"] for item in items] == [297, 298, 299]

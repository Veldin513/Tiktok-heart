from __future__ import annotations

import json
from pathlib import Path

from yara_app.project_adapter import ProjectAdapter


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

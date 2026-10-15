from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yara_app.project_adapter import ProjectAdapter


class PerformanceService:
    """Builds UI and worker performance summaries from existing local logs."""

    def __init__(self, adapter: ProjectAdapter) -> None:
        self.adapter = adapter

    @staticmethod
    def _parse_perf_line(line: str) -> dict[str, Any] | None:
        parts = line.rsplit(" ", 4)
        if len(parts) < 5:
            return None
        elapsed_text = parts[-1]
        if not elapsed_text.endswith("ms"):
            return None
        try:
            elapsed_ms = float(elapsed_text[:-2])
        except ValueError:
            return None
        return {
            "time": " ".join(parts[:2]),
            "method": parts[2],
            "path": parts[3],
            "elapsed_ms": elapsed_ms,
            "line": line,
        }

    @staticmethod
    def _status_from_worst(worst: dict[str, Any] | None, warning_count: int, *, critical_ms: float) -> str:
        if worst and float(worst.get("elapsed_ms") or 0) >= critical_ms:
            return "critical"
        if warning_count:
            return "warning"
        return "ok"

    def app_shell_summary(self) -> dict[str, Any]:
        events = [
            parsed
            for line in self.adapter.tail_file(self.adapter.app_shell_perf_log_path, lines=120)
            if (parsed := self._parse_perf_line(line)) is not None
        ]
        latest = events[-1] if events else None
        worst = max(events, key=lambda item: float(item.get("elapsed_ms") or 0), default=None)
        warning_count = sum(1 for item in events if float(item.get("elapsed_ms") or 0) >= 1000)
        return {
            "exists": self.adapter.app_shell_perf_log_path.exists(),
            "path": str(self.adapter.app_shell_perf_log_path),
            "slow_count": len(events),
            "warning_count": warning_count,
            "latest": latest,
            "worst": worst,
            "events": list(reversed(events[-8:])),
            "status": self._status_from_worst(worst, warning_count, critical_ms=2500),
        }

    def worker_phase_summary(self, profile_key: str | None = None) -> dict[str, Any]:
        history = self.adapter.get_run_history(profile_key, limit=220)
        phases = [
            item
            for item in history
            if item.get("event") == "phase_timing" and item.get("elapsed_ms") is not None
        ]
        latest = phases[-1] if phases else None
        worst = max(phases, key=lambda item: float(item.get("elapsed_ms") or 0), default=None)
        warning_count = sum(1 for item in phases if float(item.get("elapsed_ms") or 0) >= 5000)
        return {
            "phase_count": len(phases),
            "warning_count": warning_count,
            "latest": latest,
            "worst": worst,
            "events": list(reversed(phases[-8:])),
            "status": self._status_from_worst(worst, warning_count, critical_ms=15000),
        }

    def summary(self, profile_key: str | None = None) -> dict[str, Any]:
        ui = self.app_shell_summary()
        worker = self.worker_phase_summary(profile_key)
        status_rank = {"ok": 0, "warning": 1, "critical": 2}
        status = max((ui.get("status", "ok"), worker.get("status", "ok")), key=lambda item: status_rank.get(str(item), 0))
        return {**ui, "status": status, "ui": ui, "worker": worker}

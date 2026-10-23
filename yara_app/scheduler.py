from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yara_app.project_adapter import ProjectAdapter


DEFAULT_WORKER_SCHEDULE_TASK_NAME = "Yara TikTok Worker"
DEFAULT_WORKER_STARTUP_TASK_SUFFIX = " Startup"


class WorkerScheduleService:
    """Owns Windows Task Scheduler integration for the worker."""

    def __init__(
        self,
        adapter: ProjectAdapter,
        *,
        task_name: str = DEFAULT_WORKER_SCHEDULE_TASK_NAME,
        startup_task_name: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.task_name = task_name
        self.startup_task_name = startup_task_name or f"{task_name}{DEFAULT_WORKER_STARTUP_TASK_SUFFIX}"
        self._status_cache: tuple[float, dict[str, Any]] = (0.0, {})

    def register(
        self,
        *,
        at_startup: bool = True,
        at_logon: bool = False,
        every_12_hours: bool = True,
        interval_hours: int = 12,
        first_run_at: str | None = None,
    ) -> dict[str, Any]:
        if not at_startup and not at_logon and not every_12_hours:
            raise ValueError("Выбери хотя бы один триггер автозапуска worker.")
        args = [
            "-TaskName",
            self.task_name,
            "-StartupTaskName",
            self.startup_task_name,
            "-AtStartup",
            "true" if at_startup else "false",
            "-AtLogon",
            "true" if at_logon else "false",
            "-Every12Hours",
            "true" if every_12_hours else "false",
            "-IntervalHours",
            str(int(interval_hours)),
        ]
        if first_run_at:
            args.extend(["-FirstRunAt", str(first_run_at)])
        self._status_cache = (0.0, {})
        try:
            self.adapter._run_powershell_script(self.adapter.worker_schedule_register_script_path, args)
        except RuntimeError as exc:
            message = str(exc).lower()
            if any(token in message for token in ('access is denied', 'access denied', 'отказано в доступе', '0x80070005')):
                raise RuntimeError(
                    'Windows запретила изменение задачи расписания. Закройте TikTok Heart и один раз '
                    'запустите scripts/windows/register_worker_schedule.bat от имени администратора. '
                    'Затем откройте приложение обычным способом. Сохранение расписания не завершено; '
                    'настройки могли измениться частично.'
                ) from None
            raise
        status = self.status()
        self._status_cache = (time.time(), dict(status))
        return status

    def unregister(self) -> dict[str, Any]:
        self.adapter._run_powershell_script(
            self.adapter.worker_schedule_unregister_script_path,
            ["-TaskName", self.task_name, "-StartupTaskName", self.startup_task_name],
        )
        status = self.status()
        self._status_cache = (time.time(), dict(status))
        return status

    def reanchor_from_queue(self) -> dict[str, Any]:
        """Adjust only the existing repeat trigger after a run, never poll."""
        if os.name != 'nt':
            return {'updated': False, 'reason': 'unsupported'}
        status = self.status_cached(max_age_seconds=0)
        if not status.get('repeat_task_installed') or not status.get('enabled') or not status.get('every_12_hours'):
            return {'updated': False, 'reason': 'repeat_disabled'}
        queue = self.adapter.queue_snapshot()
        due = queue.get('next_send_at')
        if not due or int(queue.get('ready_count') or 0) > 0:
            return {'updated': False, 'reason': 'no_future_cooldown'}
        due = float(due)
        if due <= time.time() + 1:
            return {'updated': False, 'reason': 'already_due'}
        next_run = self.parse_timestamp(status.get('next_run_time'))
        if next_run is not None and abs(next_run - due) < 2:
            return {'updated': False, 'reason': 'already_aligned'}
        first_run_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(due))
        command = r"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '__TASK_NAME__' -ErrorAction Stop
if ([string]$task.State -eq 'Disabled') { exit 0 }
$changed = $false
foreach ($trigger in @($task.Triggers)) {
    if ($trigger.Enabled -and [string]$trigger.Repetition.Interval -eq 'PT12H') {
        $trigger.StartBoundary = ([datetime]::ParseExact('__FIRST_RUN__', 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)).ToString('yyyy-MM-ddTHH:mm:ss')
        $changed = $true
    }
}
if ($changed) {
    Set-ScheduledTask -TaskName '__TASK_NAME__' -Trigger @($task.Triggers) -ErrorAction Stop | Out-Null
}
"""
        command = command.replace('__TASK_NAME__', self.task_name.replace("'", "''")).replace('__FIRST_RUN__', first_run_at)
        self.adapter._run_powershell_command(command)
        self._status_cache = (0.0, {})
        return {'updated': True, 'first_run_at': first_run_at}

    def status_cached(self, *, max_age_seconds: float = 60.0) -> dict[str, Any]:
        timestamp, cached = self._status_cache
        if cached and (time.time() - timestamp) <= max_age_seconds:
            return dict(cached)
        status = self.status()
        self._status_cache = (time.time(), dict(status))
        return status

    @staticmethod
    def parse_timestamp(value: Any) -> float | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            with suppress(ValueError):
                return time.mktime(time.strptime(raw, fmt))
        return None

    def normalize_status(self, status: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(status)
        next_ts = self.parse_timestamp(normalized.get("next_run_time"))
        last_ts = self.parse_timestamp(normalized.get("last_run_time"))
        now = time.time()
        if next_ts is not None:
            normalized["next_run_timestamp"] = next_ts
            normalized["seconds_until_next_run"] = int(next_ts - now)
        if last_ts is not None:
            normalized["last_run_timestamp"] = last_ts
        normalized["next_run_stale"] = bool(
            normalized.get("installed")
            and normalized.get("enabled", True)
            and normalized.get("every_12_hours")
            and next_ts is not None
            and next_ts < now - 60
        )
        if normalized["next_run_stale"]:
            normalized["schedule_warning"] = "next_run_time_is_in_the_past"
        return normalized

    def _missing_status(self, *, state: str = "missing", task_name: str | None = None) -> dict[str, Any]:
        return {
            "available": True,
            "installed": False,
            "task_name": task_name or self.task_name,
            "startup_task_name": self.startup_task_name,
            "repeat_task_installed": False,
            "startup_task_installed": False,
            "state": state,
            "enabled": False,
            "at_startup": False,
            "at_logon": False,
            "every_12_hours": False,
            "interval_hours": 12,
            "triggers": [],
            "next_run_time": None,
            "last_run_time": None,
            "last_task_result": None,
            "action_execute": None,
            "action_arguments": None,
        }

    @staticmethod
    def _schtasks_name(task_name: str) -> str:
        return task_name if task_name.startswith("\\") else f"\\{task_name}"

    def _query_schtasks_task(self, task_name: str) -> dict[str, Any] | None:
        adapter = self.adapter
        if os.name != "nt":
            return None
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", self._schtasks_name(task_name), "/FO", "LIST", "/V"],
                cwd=str(adapter.base_dir),
                capture_output=True,
                check=False,
                **adapter._windows_subprocess_kwargs(hide_window=True),
            )
        except Exception:
            return None
        output = adapter._decode_process_output(result.stdout or b"").strip()
        error = adapter._decode_process_output(result.stderr or b"").strip()
        if result.returncode != 0:
            error_lower = error.lower()
            if "cannot find" in error_lower or "не удается найти" in error_lower or "not exist" in error_lower:
                return self._missing_status(task_name=task_name)
            return None
        if not output:
            return None

        rows: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in output.splitlines():
            line = raw_line.rstrip()
            if not line:
                if current:
                    rows.append(current)
                    current = {}
                continue
            if line.startswith("Folder:"):
                continue
            if ":" not in line:
                continue
            stripped = line.lstrip()
            if stripped.startswith(("Repeat", "Повторять")):
                key, value = line.rsplit(":", 1)
            else:
                key, value = line.split(":", 1)
            current[key.strip()] = value.strip()
        if current:
            rows.append(current)
        rows = [row for row in rows if row.get("TaskName") or row.get("Имя задачи")]
        if not rows:
            return None

        def pick(row: dict[str, str], *keys: str) -> str:
            for key in keys:
                value = row.get(key)
                if value and value != "N/A":
                    return value
            return ""

        def normalize_date(value: str) -> str | None:
            if not value:
                return None
            value = value.strip()
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
                with suppress(ValueError):
                    return time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(value, fmt))
            return value

        first = rows[0]
        schedule_types = [pick(row, "Schedule Type", "Тип расписания", "Schedule", "Расписание") for row in rows]
        repeat_values = [pick(row, "Repeat: Every", "Повторять: каждые") for row in rows]
        at_startup = any(
            ("startup" in value.lower())
            or ("system start" in value.lower())
            or ("boot" in value.lower())
            or ("запуск" in value.lower() and ("систем" in value.lower() or "windows" in value.lower()))
            or ("запуск" in value.lower() and "компьют" in value.lower())
            for value in schedule_types
        )
        at_logon = any(("logon" in value.lower()) or ("вход" in value.lower()) for value in schedule_types)
        every_12_hours = any(
            "12" in value and (("hour" in value.lower()) or ("час" in value.lower()) or ("ч" in value.lower()))
            for value in repeat_values
        )
        task_state = pick(first, "Scheduled Task State", "Состояние назначенной задачи")
        enabled = task_state.lower() not in {"отключено", "disabled"}
        task_to_run = pick(first, "Task To Run", "Задача для выполнения")
        action_execute, action_arguments = self._split_task_action(task_to_run)

        return {
            "available": True,
            "installed": True,
            "task_name": task_name,
            "startup_task_name": self.startup_task_name,
            "repeat_task_installed": task_name == self.task_name,
            "startup_task_installed": task_name == self.startup_task_name,
            "state": pick(first, "Status", "Состояние") or task_state or "unknown",
            "enabled": enabled,
            "at_startup": at_startup,
            "at_logon": at_logon,
            "every_12_hours": every_12_hours,
            "interval_hours": 12,
            "triggers": [
                {
                    "class": "schtasks",
                    "enabled": enabled,
                    "interval": repeat_values[index] if index < len(repeat_values) else "",
                    "text": schedule_types[index] if index < len(schedule_types) else "",
                }
                for index, _row in enumerate(rows)
            ],
            "next_run_time": normalize_date(pick(first, "Next Run Time", "Время следующего запуска")),
            "last_run_time": normalize_date(pick(first, "Last Run Time", "Время прошлого запуска")),
            "last_task_result": pick(first, "Last Result", "Прошлый результат") or None,
            "action_execute": action_execute or None,
            "action_arguments": action_arguments or None,
        }

    def _merge_task_statuses(
        self,
        repeat_status: dict[str, Any] | None,
        startup_status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        statuses = [status for status in (repeat_status, startup_status) if isinstance(status, dict)]
        installed_statuses = [status for status in statuses if status.get("installed")]
        if not statuses:
            return self._missing_status(state="unknown")
        if not installed_statuses:
            return self._missing_status()

        repeat_installed = bool(repeat_status and repeat_status.get("installed"))
        startup_installed = bool(startup_status and startup_status.get("installed"))
        base = dict(repeat_status if repeat_installed else installed_statuses[0])
        triggers: list[dict[str, Any]] = []
        for role, status in (("repeat", repeat_status), ("startup", startup_status)):
            if not status or not status.get("installed"):
                continue
            for trigger in list(status.get("triggers") or []):
                item = dict(trigger)
                item["task"] = role
                item["task_name"] = status.get("task_name")
                triggers.append(item)

        latest_run_status: dict[str, Any] | None = None
        latest_run_ts: float | None = None
        for status in installed_statuses:
            timestamp = self.parse_timestamp(status.get("last_run_time"))
            if timestamp is not None and (latest_run_ts is None or timestamp > latest_run_ts):
                latest_run_ts = timestamp
                latest_run_status = status

        repeat_next = repeat_status.get("next_run_time") if repeat_status and repeat_status.get("installed") else None
        fallback_next = next((status.get("next_run_time") for status in installed_statuses if status.get("next_run_time")), None)

        base.update({
            "available": True,
            "installed": True,
            "task_name": self.task_name,
            "startup_task_name": self.startup_task_name,
            "repeat_task_installed": repeat_installed,
            "startup_task_installed": startup_installed,
            "enabled": any(bool(status.get("enabled", True)) for status in installed_statuses),
            "at_startup": startup_installed or any(bool(status.get("at_startup")) for status in installed_statuses),
            "at_logon": any(bool(status.get("at_logon")) for status in installed_statuses),
            "every_12_hours": bool(repeat_status and repeat_status.get("every_12_hours")),
            "interval_hours": 12,
            "triggers": triggers,
            "next_run_time": repeat_next or fallback_next,
            "last_run_time": (latest_run_status or base).get("last_run_time"),
            "last_task_result": (latest_run_status or base).get("last_task_result"),
            "action_execute": base.get("action_execute"),
            "action_arguments": base.get("action_arguments"),
        })
        return base

    def _status_from_schtasks(self) -> dict[str, Any] | None:
        repeat_status = self._query_schtasks_task(self.task_name)
        startup_status = self._query_schtasks_task(self.startup_task_name)
        if repeat_status is None and startup_status is None:
            return None
        return self._merge_task_statuses(repeat_status, startup_status)

    @staticmethod
    def _split_task_action(task_to_run: str) -> tuple[str, str]:
        if not task_to_run:
            return "", ""
        stripped = task_to_run.strip()
        if stripped.startswith('"'):
            end = stripped.find('"', 1)
            if end > 1:
                return stripped[1:end], stripped[end + 1:].strip()
            return stripped.strip('"'), ""
        parts = stripped.split(" ", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    def status(self) -> dict[str, Any]:
        if os.name != "nt":
            return self.normalize_status({
                "available": False,
                "installed": False,
                "task_name": self.task_name,
                "state": "unsupported",
                "at_startup": False,
                "at_logon": False,
                "every_12_hours": False,
                "error": "Планировщик worker поддерживается только в Windows.",
            })

        fast_status = self._status_from_schtasks()
        if fast_status is not None:
            return self.normalize_status(fast_status)

        command = r"""
$ErrorActionPreference = 'Stop'
$taskName = '__TASK_NAME__'

function Format-TaskDate($Value) {
    if ($null -eq $Value) { return $null }
    try {
        $dt = [datetime]$Value
        if ($dt -eq [datetime]::MinValue) { return $null }
        return $dt.ToString('yyyy-MM-dd HH:mm:ss')
    }
    catch {
        return $null
    }
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    [pscustomobject]@{
        available = $true
        installed = $false
        task_name = $taskName
        state = 'missing'
        enabled = $false
        at_startup = $false
        at_logon = $false
        every_12_hours = $false
        interval_hours = 12
        triggers = @()
        next_run_time = $null
        last_run_time = $null
        last_task_result = $null
        action_execute = $null
        action_arguments = $null
    } | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

$info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
$triggers = @()
$atStartup = $false
$atLogon = $false
$every12 = $false

foreach ($trigger in @($task.Triggers)) {
    $class = ''
    $text = ''
    $enabled = $true
    $interval = ''
    try { $class = [string]$trigger.CimClass.CimClassName } catch {}
    try { $text = [string]$trigger } catch {}
    try { $enabled = [bool]$trigger.Enabled } catch {}
    try { $interval = [string]$trigger.Repetition.Interval } catch {}
    if ($class -like '*BootTrigger*') { $atStartup = $true }
    if ($class -like '*LogonTrigger*') { $atLogon = $true }
    if ($interval -eq 'PT12H') { $every12 = $true }
    $triggers += [pscustomobject]@{
        class = $class
        enabled = $enabled
        interval = $interval
        text = $text
    }
}

$action = @($task.Actions)[0]
[pscustomobject]@{
    available = $true
    installed = $true
    task_name = $taskName
    state = [string]$task.State
    enabled = ([string]$task.State -ne 'Disabled')
    at_startup = $atStartup
    at_logon = $atLogon
    every_12_hours = $every12
    interval_hours = 12
    triggers = $triggers
    next_run_time = Format-TaskDate $info.NextRunTime
    last_run_time = Format-TaskDate $info.LastRunTime
    last_task_result = $info.LastTaskResult
    action_execute = [string]$action.Execute
    action_arguments = [string]$action.Arguments
} | ConvertTo-Json -Depth 6 -Compress
"""
        try:
            command = command.replace("__TASK_NAME__", self.task_name.replace("'", "''"))
            result = self.adapter._run_powershell_command(command)
            payload = (result.stdout or "").strip()
            data = json.loads(payload) if payload else {}
            if isinstance(data, dict):
                return self.normalize_status(data)
        except Exception as exc:
            status = self._missing_status(state="error")
            status["error"] = str(exc)
            return self.normalize_status(status)
        return self.normalize_status(self._missing_status(state="unknown"))

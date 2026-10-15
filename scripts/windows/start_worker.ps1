param(
    [switch]$SelfTest,
    [switch]$NoBrowser,
    [switch]$BootForce
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$logsDir = Join-Path $repoRoot "logs"
$logPath = Join-Path $logsDir "worker_scheduler.log"
$starter = Join-Path $repoRoot "scripts\start_worker_once.py"
$scheduleRegister = Join-Path $scriptDir "register_worker_schedule.ps1"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-SchedulerLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp $Message"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        try {
            $stream = [System.IO.File]::Open($logPath, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
            try {
                $writer = New-Object System.IO.StreamWriter($stream, $encoding)
                try {
                    $writer.WriteLine($line)
                }
                finally {
                    $writer.Dispose()
                }
            }
            finally {
                if ($null -ne $stream) {
                    $stream.Dispose()
                }
            }
            return
        }
        catch {
            if ($attempt -eq 4) {
                return
            }
            Start-Sleep -Milliseconds (50 * ($attempt + 1))
        }
    }
}

function Resolve-Python {
    $candidates = @()
    if ($env:YARA_PYTHON) {
        $candidates += $env:YARA_PYTHON
    }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.13-64\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.12-64\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python314\python.exe"),
        (Join-Path $env:ProgramFiles "Python313\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source) -and ($command.Source -notmatch "\\WindowsApps\\")) {
        return $command.Source
    }

    throw "Python executable was not found. Set YARA_PYTHON to a Python with project dependencies."
}

function Resolve-QueueFirstRunAt {
    param([string]$PythonPath)

    $code = @'
from yara_app.project_adapter import ProjectAdapter

adapter = ProjectAdapter()
queue = adapter.queue_snapshot()
if int(queue.get("target_count") or 0) > 0 and int(queue.get("ready_count") or 0) == 0:
    text = str(queue.get("next_send_at_text") or "").strip()
    if text:
        print(text)
'@
    $tempScript = Join-Path $logsDir ("schedule_anchor_{0}.py" -f ([guid]::NewGuid().ToString("N")))
    try {
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tempScript, $code, $encoding)
        Push-Location $repoRoot
        try {
            $previousErrorActionPreference = $ErrorActionPreference
            $previousPythonPath = $env:PYTHONPATH
            $ErrorActionPreference = "Continue"
            if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
                $env:PYTHONPATH = $repoRoot
            }
            else {
                $env:PYTHONPATH = "$repoRoot;$env:PYTHONPATH"
            }
            $output = & $PythonPath $tempScript 2>&1
            $exitCode = $LASTEXITCODE
            $env:PYTHONPATH = $previousPythonPath
            $ErrorActionPreference = $previousErrorActionPreference
        }
        finally {
            if ($null -ne $previousPythonPath) {
                $env:PYTHONPATH = $previousPythonPath
            }
            elseif (Test-Path Env:PYTHONPATH) {
                Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
            }
            if ($null -ne $previousErrorActionPreference) {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            Pop-Location
        }
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
        if ($exitCode -ne 0) {
            Write-SchedulerLog "Schedule anchor lookup failed with exit code $exitCode"
            foreach ($line in @($output | Select-Object -First 4)) {
                Write-SchedulerLog "[anchor] $([string]$line)"
            }
            return ""
        }
        foreach ($line in $output) {
            $text = ([string]$line).Trim()
            if ($text) {
                return $text
            }
        }
    }
    catch {
        Write-SchedulerLog "Schedule anchor lookup failed: $($_.Exception.Message)"
    }
    finally {
        if (Test-Path -LiteralPath $tempScript) {
            Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
        }
    }
    return ""
}

function Test-StarterEvent {
    param(
        [object[]]$StarterOutput,
        [string]$Event
    )

    foreach ($line in @($StarterOutput)) {
        if (([string]$line).Contains("[starter] $Event")) {
            return $true
        }
    }
    return $false
}

function Refresh-WorkerSchedule {
    param(
        [string]$PythonPath,
        [object[]]$StarterOutput
    )

    if ($SelfTest) {
        return
    }
    if (-not (Test-Path -LiteralPath $scheduleRegister)) {
        Write-SchedulerLog "Schedule refresh skipped: register script was not found."
        return
    }
    try {
        $scheduleArgs = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $scheduleRegister,
            "-TaskName", "Yara TikTok Worker",
            "-AtStartup", "true",
            "-AtLogon", "false",
            "-Every12Hours", "true",
            "-IntervalHours", "12"
        )
        $workerStarted = (Test-StarterEvent $StarterOutput "worker_started") -or (Test-StarterEvent $StarterOutput "skip_worker_running")
        if ($workerStarted) {
            $firstRunAt = (Get-Date).AddHours(12).ToString("yyyy-MM-dd HH:mm:ss")
            Write-SchedulerLog "Schedule anchor: starter event -> $firstRunAt"
        }
        else {
            $firstRunAt = Resolve-QueueFirstRunAt $PythonPath
            if ($firstRunAt) {
                Write-SchedulerLog "Schedule anchor: queue -> $firstRunAt"
            }
        }
        if ($firstRunAt) {
            $scheduleArgs += @("-FirstRunAt", $firstRunAt)
        }
        $scheduleOutput = & powershell.exe @scheduleArgs 2>&1
        $scheduleExitCode = $LASTEXITCODE
        foreach ($line in $scheduleOutput) {
            Write-SchedulerLog "[schedule] $([string]$line)"
        }
        if ($null -eq $scheduleExitCode) {
            $scheduleExitCode = 0
        }
        if ($scheduleExitCode -ne 0) {
            Write-SchedulerLog "Schedule refresh failed with exit code $scheduleExitCode"
        }
    }
    catch {
        Write-SchedulerLog "Schedule refresh failed: $($_.Exception.Message)"
    }
}

try {
    $python = Resolve-Python
    Write-SchedulerLog "Starting worker via $python"
    $starterArgs = @()
    if ($SelfTest) {
        $starterArgs += "--self-test"
    }
    if ($NoBrowser) {
        $starterArgs += "--no-browser"
    }
    if ($BootForce) {
        $starterArgs += "--boot-force"
        Write-SchedulerLog "Startup cooldown bypass requested by Windows startup task."
    }
    Push-Location $repoRoot
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $output = & $python $starter @starterArgs 2>&1
        $exitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorActionPreference
    }
    finally {
        if ($null -ne $previousErrorActionPreference) {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        Pop-Location
    }
    foreach ($line in $output) {
        Write-SchedulerLog ([string]$line)
    }
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
    Refresh-WorkerSchedule $python $output
    Write-SchedulerLog "Starter finished with exit code $exitCode"
    exit $exitCode
}
catch {
    Write-SchedulerLog "ERROR: $($_.Exception.Message)"
    exit 1
}

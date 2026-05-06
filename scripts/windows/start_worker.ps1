$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$logsDir = Join-Path $repoRoot "logs"
$logPath = Join-Path $logsDir "worker_scheduler.log"
$starter = Join-Path $repoRoot "scripts\start_worker_once.py"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-SchedulerLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Encoding UTF8 -Value "$timestamp $Message"
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
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source)) {
        return $command.Source
    }

    throw "Python executable was not found. Set YARA_PYTHON to a Python with project dependencies."
}

try {
    $python = Resolve-Python
    Write-SchedulerLog "Starting worker via $python"
    Push-Location $repoRoot
    try {
        $output = & $python $starter 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    foreach ($line in $output) {
        Write-SchedulerLog $line
    }
    Write-SchedulerLog "Starter finished with exit code $exitCode"
    exit $exitCode
}
catch {
    Write-SchedulerLog "ERROR: $($_.Exception.Message)"
    exit 1
}

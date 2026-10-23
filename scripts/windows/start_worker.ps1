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
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
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
    Write-SchedulerLog "Starter finished with exit code $exitCode"
    exit $exitCode
}
catch {
    Write-SchedulerLog "ERROR: $($_.Exception.Message)"
    exit 1
}

param(
    [string]$ChromeProfile = "Default",
    [string]$BotProfileName = ""
)

$ErrorActionPreference = "Stop"
if (-not $BotProfileName) {
    $BotProfileName = if ($env:TIKTOK_BOT_PROFILE) { $env:TIKTOK_BOT_PROFILE } else { "default" }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$python = $env:YARA_PYTHON
if (-not $python -or -not (Test-Path -LiteralPath $python)) {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python executable was not found. Set YARA_PYTHON to a Python with project dependencies.'
}

& $python (Join-Path $repoRoot 'scripts\import_chrome_session.py') --chrome-profile $ChromeProfile --bot-profile $BotProfileName
exit $LASTEXITCODE

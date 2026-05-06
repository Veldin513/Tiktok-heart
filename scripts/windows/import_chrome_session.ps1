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
$sourceRoot = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
$sourceDefault = Join-Path $sourceRoot $ChromeProfile
$destRoot = Join-Path $repoRoot "profiles\$BotProfileName\browser\user_data"
$stateBackoff = Join-Path $repoRoot "profiles\$BotProfileName\state\auth_backoff.json"

if (-not (Test-Path -LiteralPath $sourceDefault)) {
    throw "Chrome Default profile was not found: $sourceDefault"
}

$chromeProcesses = Get-Process -Name chrome -ErrorAction SilentlyContinue
if ($chromeProcesses) {
    Write-Host "Close all Chrome windows first, then run this script again."
    Write-Host "Chrome is still running with PID(s): $($chromeProcesses.Id -join ', ')"
    exit 2
}

$destParent = Split-Path -Parent $destRoot
New-Item -ItemType Directory -Force -Path $destParent | Out-Null

if (Test-Path -LiteralPath $destRoot) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupRoot = Join-Path $destParent "user_data_before_chrome_import_$timestamp"
    Move-Item -LiteralPath $destRoot -Destination $backupRoot
    Write-Host "Bot profile backup: $backupRoot"
}

New-Item -ItemType Directory -Force -Path $destRoot | Out-Null

$rootFiles = @("Local State", "Last Version", "First Run")
foreach ($name in $rootFiles) {
    $source = Join-Path $sourceRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $destRoot $name) -Force
    }
}

Copy-Item -LiteralPath $sourceDefault -Destination (Join-Path $destRoot "Default") -Recurse -Force

Get-ChildItem -LiteralPath $destRoot -Force -Filter "Singleton*" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

if (Test-Path -LiteralPath $stateBackoff) {
    Remove-Item -LiteralPath $stateBackoff -Force
}

Write-Host "Chrome session imported into bot profile:"
Write-Host $destRoot
Write-Host "Now start the bot. Keep ordinary Chrome closed while the bot is running."

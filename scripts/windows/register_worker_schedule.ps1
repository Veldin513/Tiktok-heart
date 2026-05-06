param(
    [string]$TaskName = "Yara TikTok Worker",
    [object]$AtLogon = $true,
    [object]$Every12Hours = $true,
    [int]$IntervalHours = 12
)

$ErrorActionPreference = "Stop"

function Convert-Flag {
    param(
        [object]$Value,
        [string]$Name
    )

    if ($Value -is [bool]) {
        return [bool]$Value
    }

    $text = ([string]$Value).Trim().ToLowerInvariant()
    switch ($text) {
        "true"  { return $true }
        "1"     { return $true }
        "yes"   { return $true }
        "on"    { return $true }
        "false" { return $false }
        "0"     { return $false }
        "no"    { return $false }
        "off"   { return $false }
        default { throw "Invalid boolean value for ${Name}: $Value" }
    }
}

$AtLogonEnabled = Convert-Flag $AtLogon "AtLogon"
$Every12HoursEnabled = Convert-Flag $Every12Hours "Every12Hours"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$starter = Join-Path $scriptDir "start_worker.vbs"

if (-not (Test-Path -LiteralPath $starter)) {
    throw "Worker starter was not found: $starter"
}

if (-not $AtLogonEnabled -and -not $Every12HoursEnabled) {
    throw "Select at least one trigger: -AtLogon `$true or -Every12Hours `$true."
}

if ($IntervalHours -lt 1) {
    throw "IntervalHours must be greater than zero."
}

$argument = "`"$starter`""
$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument $argument `
    -WorkingDirectory $repoRoot

$triggers = @()
$triggerLabels = @()

if ($AtLogonEnabled) {
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $triggerLabels += "at user logon"
}

if ($Every12HoursEnabled) {
    $repeatStart = (Get-Date).AddHours($IntervalHours)
    $triggers += New-ScheduledTaskTrigger `
        -Once `
        -At $repeatStart `
        -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $triggerLabels += "every $IntervalHours hours"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts Yara TikTok worker: $($triggerLabels -join ', ')." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Action: wscript.exe $argument"
Write-Host "Triggers: $($triggerLabels -join ', ')"

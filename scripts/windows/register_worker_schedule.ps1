param(
    [string]$TaskName = "Yara TikTok Worker",
    [string]$StartupTaskName = "",
    [object]$AtStartup = $true,
    [object]$AtLogon = $false,
    [object]$Every12Hours = $true,
    [int]$IntervalHours = 12,
    [string]$FirstRunAt = ""
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

$AtStartupEnabled = Convert-Flag $AtStartup "AtStartup"
$AtLogonEnabled = Convert-Flag $AtLogon "AtLogon"
$Every12HoursEnabled = Convert-Flag $Every12Hours "Every12Hours"

function Resolve-RepeatStart {
    param(
        [string]$Value,
        [int]$Hours
    )

    $now = Get-Date
    $text = ([string]$Value).Trim()
    if ($text) {
        $parsed = [datetime]::MinValue
        $culture = [Globalization.CultureInfo]::InvariantCulture
        $styles = [Globalization.DateTimeStyles]::AssumeLocal
        $formats = @(
            "yyyy-MM-dd HH:mm:ss",
            "yyyy-MM-dd HH:mm",
            "yyyy-MM-ddTHH:mm:ss",
            "dd.MM.yyyy HH:mm:ss",
            "dd.MM.yyyy HH:mm"
        )
        $ok = [datetime]::TryParseExact($text, $formats, $culture, $styles, [ref]$parsed)
        if (-not $ok) {
            $ok = [datetime]::TryParse($text, $culture, $styles, [ref]$parsed)
        }
        if (-not $ok) {
            throw "Invalid FirstRunAt value: $Value"
        }
        if ($parsed -gt $now.AddSeconds(5)) {
            return $parsed
        }
    }

    return $now.AddHours($Hours)
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$starter = Join-Path $scriptDir "start_worker.vbs"

if (-not (Test-Path -LiteralPath $starter)) {
    throw "Worker starter was not found: $starter"
}

if ([string]::IsNullOrWhiteSpace($StartupTaskName)) {
    $StartupTaskName = "$TaskName Startup"
}

if (-not $AtStartupEnabled -and -not $AtLogonEnabled -and -not $Every12HoursEnabled) {
    throw "Select at least one trigger: -AtStartup `$true, -AtLogon `$true or -Every12Hours `$true."
}

if ($IntervalHours -lt 1) {
    throw "IntervalHours must be greater than zero."
}

function New-WorkerAction {
    param([string]$Arguments)

    return New-ScheduledTaskAction `
        -Execute "wscript.exe" `
        -Argument $Arguments `
        -WorkingDirectory $repoRoot
}

function Remove-WorkerTaskIfExists {
    param([string]$Name)

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed scheduled task: $Name"
    }
}

function Grant-WorkerTaskAccess {
    param([string]$Name)

    # Tasks registered once from an elevated window must remain editable by
    # this same user when the app/worker runs with normal privileges.
    if (-not (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)) { return }
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $task = $service.GetFolder("\").GetTask($Name)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = New-Object Security.AccessControl.RawSecurityDescriptor($task.GetSecurityDescriptor(4))
    $fileAllAccess = 0x1F01FF
    foreach ($existingAce in $security.DiscretionaryAcl) {
        if ($existingAce -is [Security.AccessControl.CommonAce] -and
            $existingAce.SecurityIdentifier -eq $sid -and
            $existingAce.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            ($existingAce.AccessMask -band $fileAllAccess) -eq $fileAllAccess) { return }
    }
    $ace = New-Object Security.AccessControl.CommonAce(
        [Security.AccessControl.AceFlags]::None,
        [Security.AccessControl.AceQualifier]::AccessAllowed,
        $fileAllAccess, $sid, $false, $null
    )
    $security.DiscretionaryAcl.InsertAce(0, $ace)
    $task.SetSecurityDescriptor($security.GetSddlForm([Security.AccessControl.AccessControlSections]::Access), 0)
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

$registered = @()
$baseArgument = "`"$starter`""
$startupArgument = "`"$starter`" --boot-force"

if ($Every12HoursEnabled) {
    $repeatStart = Resolve-RepeatStart $FirstRunAt $IntervalHours
    $repeatTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At $repeatStart `
        -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action (New-WorkerAction $baseArgument) `
        -Trigger @($repeatTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Description "TikTok Heart: every $IntervalHours hours; next run follows the actual send cooldown. No background polling." `
        -Force | Out-Null
    Grant-WorkerTaskAccess $TaskName
    $registered += "$($TaskName): every $IntervalHours hours"
}
else {
    Remove-WorkerTaskIfExists $TaskName
}

if ($AtStartupEnabled -or $AtLogonEnabled) {
    $startupTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    try {
        $startupTrigger.Delay = "PT2M"
    }
    catch {
        # Older Windows builds can omit Delay on logon triggers.
    }

    Register-ScheduledTask `
        -TaskName $StartupTaskName `
        -Action (New-WorkerAction $startupArgument) `
        -Trigger @($startupTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Description "Starts TikTok Heart worker once after Windows sign-in and bypasses cooldown for that boot session." `
        -Force | Out-Null
    Grant-WorkerTaskAccess $StartupTaskName
    $registered += "$($StartupTaskName): Windows sign-in startup"
}
else {
    Remove-WorkerTaskIfExists $StartupTaskName
}

Write-Host "Registered scheduled tasks: $($registered -join '; ')"
Write-Host "Repeat action: wscript.exe $baseArgument"
Write-Host "Startup action: wscript.exe $startupArgument"
if ($Every12HoursEnabled) {
    Write-Host "12h first run: $($repeatStart.ToString('yyyy-MM-dd HH:mm:ss'))"
}

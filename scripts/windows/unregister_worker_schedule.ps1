param(
    [string]$TaskName = "Yara TikTok Worker",
    [string]$StartupTaskName = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($StartupTaskName)) {
    $StartupTaskName = "$TaskName Startup"
}

foreach ($name in @($TaskName, $StartupTaskName)) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed scheduled task: $name"
    }
    else {
        Write-Host "Scheduled task was not found: $name"
    }
}

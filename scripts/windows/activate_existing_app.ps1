$ErrorActionPreference = "SilentlyContinue"

$process = Get-Process -Name "yara-control" -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and $_.Responding } |
    Sort-Object StartTime |
    Select-Object -First 1

if (-not $process) {
    exit 1
}

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class WindowTools {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
"@

[WindowTools]::ShowWindowAsync($process.MainWindowHandle, 9) | Out-Null
[WindowTools]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
exit 0

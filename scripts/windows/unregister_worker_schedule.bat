@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0unregister_worker_schedule.ps1" %*

echo.
pause
endlocal

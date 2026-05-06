@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_worker_schedule.ps1" %*
if errorlevel 1 (
  echo.
  echo Failed to register worker schedule. Read the message above.
  pause
  exit /b 1
)

echo.
pause
endlocal

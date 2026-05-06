@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0import_chrome_session.ps1" %*
if errorlevel 1 (
  echo.
  echo Import failed. Read the message above, then try again.
  pause
  exit /b 1
)

echo.
pause
endlocal

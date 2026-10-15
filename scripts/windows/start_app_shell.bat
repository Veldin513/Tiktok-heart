@echo off
setlocal

for %%I in ("%~dp0..\..") do set "ROOT=%%~fI\"

cd /d "%ROOT%"
call "%~dp0python_env.bat"

set "PORT_FILE=%ROOT%control\app_shell_server.json"
set "EXISTING_URL="
if exist "%PORT_FILE%" (
  for /f "usebackq delims=" %%U in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $state = Get-Content -Raw -LiteralPath $env:PORT_FILE | ConvertFrom-Json; if ($state.url) { [string]$state.url } } catch {}"`) do set "EXISTING_URL=%%U"
)

if defined EXISTING_URL (
  curl.exe -fsS --max-time 1 "%EXISTING_URL%api/live" >nul 2>nul
  if not errorlevel 1 (
    start "" "%EXISTING_URL%"
    goto :done
  )
)

curl.exe -fsS --max-time 1 http://127.0.0.1:5874/api/live >nul 2>nul
if not errorlevel 1 (
  start "" http://127.0.0.1:5874/
  goto :done
)

if defined PYW (
  start "" "%PYW%" "%ROOT%app_shell\server.py" --open-browser
) else if defined PY (
  start "TikTok Heart" /min "%PY%" "%ROOT%app_shell\server.py" --open-browser
) else (
  echo Python executable was not found. Set YARA_PYTHON to a Python with project dependencies.
  pause
)

:done
endlocal
exit /b 0

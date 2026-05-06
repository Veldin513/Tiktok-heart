@echo off
setlocal

for %%I in ("%~dp0..\..") do set "ROOT=%%~fI\"
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"
set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"

cd /d "%ROOT%"

set "PORT_FILE=%ROOT%control\app_shell_server.json"
set "EXISTING_URL="
if exist "%PORT_FILE%" (
  for /f "usebackq delims=" %%U in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $state = Get-Content -Raw -LiteralPath $env:PORT_FILE | ConvertFrom-Json; if ($state.url) { [string]$state.url } } catch {}"`) do set "EXISTING_URL=%%U"
)

if defined EXISTING_URL (
  curl.exe -fsS --max-time 1 "%EXISTING_URL%api/diagnostics" >nul 2>nul
  if not errorlevel 1 (
    start "" "%EXISTING_URL%"
    goto :done
  )
)

curl.exe -fsS --max-time 1 http://127.0.0.1:5874/api/diagnostics >nul 2>nul
if not errorlevel 1 (
  start "" http://127.0.0.1:5874/
  goto :done
)

if exist "%PYW%" (
  start "" "%PYW%" "%ROOT%app_shell\server.py" --open-browser
) else (
  start "TikTok Heart" /min "%PY%" "%ROOT%app_shell\server.py" --open-browser
)

:done
endlocal

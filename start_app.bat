@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "YARA_PROJECT_ROOT=%~dp0"
call "%~dp0scripts\windows\python_env.bat"
if defined PY set "YARA_PYTHON=%PY%"
if exist "%~dp0scripts\windows\activate_existing_app.ps1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\activate_existing_app.ps1" >nul 2>nul
  if not errorlevel 1 goto :done
)

if exist "%~dp0src-tauri\target\release\yara-control.exe" (
  start "" "%~dp0src-tauri\target\release\yara-control.exe"
  goto :done
)

if exist "%~dp0src-tauri\target\debug\yara-control.exe" (
  start "" "%~dp0src-tauri\target\debug\yara-control.exe"
  goto :done
)

if exist "%~dp0release\" (
  set "RELEASE_EXE="
  for /f "usebackq delims=" %%E in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0release' -Recurse -Filter 'yara-control.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName"`) do (
    set "RELEASE_EXE=%%E"
  )
  if defined RELEASE_EXE (
    start "" "!RELEASE_EXE!"
    goto :done
  )
)

if exist "%~dp0node_modules\.bin\tauri.cmd" (
  call "%~dp0scripts\windows\start_yara_tauri.bat"
  goto :done
)

call "%~dp0scripts\windows\start_app_shell.bat"

:done
endlocal
exit /b 0

@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0src-tauri\target\release\yara-control.exe" (
  start "" "%~dp0src-tauri\target\release\yara-control.exe"
  goto :done
)

if exist "%~dp0src-tauri\target\debug\yara-control.exe" (
  start "" "%~dp0src-tauri\target\debug\yara-control.exe"
  goto :done
)

if exist "%~dp0release\" (
  for /r "%~dp0release" %%E in (yara-control.exe) do (
    if exist "%%E" (
      start "" "%%E"
      goto :done
    )
  )
)

if exist "%~dp0node_modules\.bin\tauri.cmd" (
  call "%~dp0scripts\windows\start_yara_tauri.bat"
  goto :done
)

call "%~dp0scripts\windows\start_app_shell.bat"

:done
endlocal

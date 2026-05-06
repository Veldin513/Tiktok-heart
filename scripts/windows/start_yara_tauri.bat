@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
set "PATH=%ProgramFiles%\Git\cmd;%USERPROFILE%\.cargo\bin;%LOCALAPPDATA%\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.15.0-win-x64;%PATH%"
if exist "src-tauri\target\release\yara-control.exe" (
  start "" "src-tauri\target\release\yara-control.exe"
) else if exist "src-tauri\target\debug\yara-control.exe" (
  start "" "src-tauri\target\debug\yara-control.exe"
) else if exist "node_modules\.bin\tauri.cmd" (
  npm run dev
) else (
  call "scripts\windows\start_app_shell.bat"
)

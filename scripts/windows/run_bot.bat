@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_worker.ps1"
endlocal

@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
call "scripts\windows\start_app_shell.bat"
endlocal

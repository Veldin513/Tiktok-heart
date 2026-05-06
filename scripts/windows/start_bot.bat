@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
if not exist control mkdir control
python -m yara_app.launcher
endlocal

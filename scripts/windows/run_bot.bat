@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
python -m yara_app.tiktok_checker
endlocal

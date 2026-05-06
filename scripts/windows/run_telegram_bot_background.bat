@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
if not exist control mkdir control
if exist "%LocalAppData%\Programs\Python\Python313\pythonw.exe" (
  start "telegram-bot-v2" /min "%LocalAppData%\Programs\Python\Python313\pythonw.exe" -m yara_app.telegram_control_bot
  exit /b
)
start "telegram-bot-v2" /min pythonw.exe -m yara_app.telegram_control_bot
endlocal

@echo off
setlocal
cd /d "%~dp0"
if not exist control mkdir control
if exist "%LocalAppData%\Programs\Python\Python313\pythonw.exe" (
  start "telegram-bot-v2" /min "%LocalAppData%\Programs\Python\Python313\pythonw.exe" "%~dp0telegram_control_bot.py"
  exit /b
)
start "telegram-bot-v2" /min pythonw.exe "%~dp0telegram_control_bot.py"

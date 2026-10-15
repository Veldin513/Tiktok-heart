@echo off
setlocal
for %%I in ("%~dp0..\..") do cd /d "%%~fI"
if not exist control mkdir control
call "%~dp0python_env.bat"
if defined PYW (
  start "telegram-bot-v2" /min "%PYW%" -m yara_app.telegram_control_bot
  goto :done
)
if defined PY (
  start "telegram-bot-v2" /min "%PY%" -m yara_app.telegram_control_bot
  goto :done
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
  start "telegram-bot-v2" /min pythonw.exe -m yara_app.telegram_control_bot
  goto :done
)
echo Python executable was not found. Set YARA_PYTHON to a Python with project dependencies.
pause

:done
endlocal
exit /b

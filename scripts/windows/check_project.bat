@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do cd /d "%%~fI"

call "%~dp0python_env.bat"
if not defined PY (
  echo Python executable was not found. Set YARA_PYTHON to a Python with project dependencies.
  exit /b 1
)

set "MODE=%~1"
if /i "%MODE%"=="lint" goto :lint
if /i "%MODE%"=="test" goto :test
if /i "%MODE%"=="js" goto :js

call :lint || exit /b 1
call :test || exit /b 1
call :js || exit /b 1
exit /b 0

:lint
"%PY%" -m ruff check .
exit /b %ERRORLEVEL%

:test
"%PY%" -m pytest
exit /b %ERRORLEVEL%

:js
node --check app_shell\web\app.js
exit /b %ERRORLEVEL%

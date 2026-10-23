@echo off
rem Shared Python discovery for Windows launchers. Sets PY and PYW in the caller scope.
set "PY="
set "PYW="

if defined YARA_PYTHON if exist "%YARA_PYTHON%" set "PY=%YARA_PYTHON%"
for %%I in ("%~dp0..\..") do if not defined PY if exist "%%~fI\.venv\Scripts\python.exe" set "PY=%%~fI\.venv\Scripts\python.exe"

for %%P in (
  "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%ProgramFiles%\Python314\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
) do (
  if not defined PY if exist "%%~P" set "PY=%%~P"
)

if not defined PY (
  for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    echo %%P | findstr /i "\\WindowsApps\\" >nul
    if errorlevel 1 if not defined PY set "PY=%%P"
  )
)

if defined PY (
  for %%I in ("%PY%") do if exist "%%~dpIpythonw.exe" set "PYW=%%~dpIpythonw.exe"
)

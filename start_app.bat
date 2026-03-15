@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0start_app.vbs" (
  wscript.exe "%~dp0start_app.vbs"
  exit /b
)
pyw -3.14 "%~dp0desktop_app.py" >nul 2>nul && exit /b
pyw -3 "%~dp0desktop_app.py" >nul 2>nul && exit /b
if exist "%LocalAppData%\Python\pythoncore-3.14-64\pythonw.exe" (
  start "" "%LocalAppData%\Python\pythoncore-3.14-64\pythonw.exe" "%~dp0desktop_app.py"
  exit /b
)
if exist "%LocalAppData%\Programs\Python\Python314\pythonw.exe" (
  start "" "%LocalAppData%\Programs\Python\Python314\pythonw.exe" "%~dp0desktop_app.py"
  exit /b
)
start "" py -3.14 "%~dp0desktop_app.py"

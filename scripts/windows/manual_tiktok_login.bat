@echo off
setlocal

for %%I in ("%~dp0..\..") do set "ROOT=%%~fI\"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE_NAME=%~1"
if "%PROFILE_NAME%"=="" set "PROFILE_NAME=%TIKTOK_BOT_PROFILE%"
if "%PROFILE_NAME%"=="" set "PROFILE_NAME=default"
set "PROFILE=%ROOT%profiles\%PROFILE_NAME%\browser\user_data"

if not exist "%CHROME%" (
  echo Chrome was not found at "%CHROME%".
  echo Install Google Chrome or update this script with the correct path.
  pause
  exit /b 1
)

echo Close the bot and all Chrome windows that use this profile before logging in.
echo After TikTok login succeeds, close this Chrome window and start the bot again.
start "" "%CHROME%" --user-data-dir="%PROFILE%" --profile-directory=Default "https://www.tiktok.com/login?redirect_url=https%%3A%%2F%%2Fwww.tiktok.com%%2Fmessages"

endlocal

@echo off
REM Opens the VR Agent room in its own browser window with sound allowed
REM automatically (no "click to enable audio"). Start the server first.
REM Capture this window in OBS, or better, use an OBS Browser Source instead.
setlocal
set "URL=http://127.0.0.1:12393/vr-agent/room.html"
if not "%~1"=="" set "URL=%~1"
set "PROFILE=%LOCALAPPDATA%\VRAgentRoomBrowser"
set "FLAGS=--autoplay-policy=no-user-gesture-required --user-data-dir="%PROFILE%" --no-first-run --window-size=1920,1080 --app=%URL%"

set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if exist "%CHROME%" (
  start "" "%CHROME%" %FLAGS%
  goto :eof
)
if exist "%EDGE%" (
  start "" "%EDGE%" %FLAGS%
  goto :eof
)
echo Could not find Chrome or Edge. Open %URL% in OBS as a Browser Source instead.
pause

@echo off
setlocal
cd /d "%~dp0"
title Appointment Booker — local app

if not exist ".venv\Scripts\python.exe" (
  echo [FAIL] Setup not done — run SETUP.bat first.
  pause & exit /b 1
)
set "VENV=.venv\Scripts\python.exe"

:: Is a server already running on the port? If so, just open the dashboard.
powershell -NoProfile -Command "$r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 }" >nul 2>&1
if %errorlevel%==0 (
  echo A server is already running — opening the dashboard.
  start "" "http://127.0.0.1:8765/"
  exit /b 0
)

echo Starting Appointment Booker ...
echo   - The dashboard opens in your browser (a token in the URL signs you in).
echo   - Keep this window open while the app runs.
echo   - Keep the PC awake and stay signed in on the portal tab in Chrome.
echo.
"%VENV%" -m app.localserver
echo.
echo Server stopped. Your queue is saved in data\queue.db — run START.bat to resume.
pause

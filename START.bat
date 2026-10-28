@echo off
setlocal
cd /d "%~dp0"
title Appointment Booker - local app

if not exist ".venv\Scripts\python.exe" (
  echo [FAIL] Setup was not run yet - run SETUP.bat first.
  pause
  exit /b 1
)
set "VENV=.venv\Scripts\python.exe"

:: Already running? Check for OUR app on :8080 (not just any HTTP 200).
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/' -TimeoutSec 2; if ($r.Content -match 'Appointment Booker') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo Appointment Booker is already running - opening the dashboard.
  set "TOKEN="
  if exist "data\app-token" (set /p TOKEN=<"data\app-token")
  if defined TOKEN (
    start "" "http://127.0.0.1:8080/?token=%TOKEN%"
  ) else (
    start "" "http://127.0.0.1:8080/"
  )
  exit /b 0
)

echo Starting Appointment Booker ...
echo   - The dashboard opens in your browser (token in the URL signs you in).
echo   - Keep this window open while the app runs.
echo   - Keep the PC awake and stay signed in on the portal tab in Chrome.
echo.
"%VENV%" -m app.localserver
if not %errorlevel%==0 (
  echo.
  echo [FAIL] The server exited with an error (see above).
)
echo.
echo Server stopped. Your queue is saved in data\queue.db - run START.bat to resume.
pause

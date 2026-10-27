@echo off
setlocal
cd /d "%~dp0"
echo === Appointment Booker — SETUP ===
echo.

:: Locate a supported Python (3.11+)
set "PY="
where py >nul 2>nul && (py -3.11 -c "print('ok')" >nul 2>nul && set "PY=py -3.11")
if not defined PY (where python >nul 2>nul && python -c "import sys;exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PY=python")
if not defined PY (
  echo [FAIL] Python 3.11+ not found.
  echo   Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
  echo   then run SETUP.bat again. No admin rights are needed.
  exit /b 1
)
echo Using: %PY%

:: Virtual environment (local, no admin needed)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment .venv ...
  %PY% -m venv .venv || (echo [FAIL] venv creation failed & exit /b 1)
)
set "VENV=.venv\Scripts\python.exe"

echo Upgrading pip + installing dependencies ...
"%VENV%" -m pip install --upgrade pip >nul 2>&1 || (echo [FAIL] pip upgrade failed & exit /b 1)
"%VENV%" -m pip install -r requirements.txt || (echo [FAIL] pip install failed & exit /b 1)
"%VENV%" -m playwright install chromium >nul 2>&1 || echo [warn] playwright browser install skipped/failed (only needed for the hosted-browser path, not the extension app)

if not exist "data" mkdir "data"
echo.
echo === SETUP DONE ===
echo.
echo Next, load the Chrome extension (one time):
echo   1. Open  chrome://extensions  in Chrome
echo   2. Turn on  Developer mode  (top-right)
echo   3. Click  "Load unpacked"  and pick this folder:
echo      %CD%\extension
echo   4. The extension icon appears in the toolbar.
echo.
echo Then run START.bat to open the dashboard.
pause

@echo off
setlocal
cd /d "%~dp0"
echo === Appointment Booker - SETUP ===
echo.

set "PY="
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -c "print('ok')" >nul 2>nul
  if %errorlevel%==0 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY=python"
)
if not defined PY (
  echo [FAIL] Python 3.11+ was not found.
  echo   Install it from https://www.python.org/downloads/
  echo   Tick "Add python.exe to PATH" during install - no admin needed.
  echo   Then run SETUP.bat again.
  pause
  exit /b 1
)
echo Using: %PY%

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not %errorlevel%==0 (
  echo [FAIL] Your Python is older than 3.11. Install a newer one from
  echo   https://www.python.org/downloads/ and re-run SETUP.bat.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment .venv ...
  %PY% -m venv .venv
  if not %errorlevel%==0 (
    echo [FAIL] Could not create the virtual environment.
    pause
    exit /b 1
  )
)
set "VENV=.venv\Scripts\python.exe"

echo Installing dependencies (this can take a minute) ...
"%VENV%" -m pip install --upgrade pip >nul 2>&1
if not %errorlevel%==0 (
  echo [FAIL] pip upgrade failed.
  pause
  exit /b 1
)
if exist "wheels\" (
  echo Using bundled wheels (no internet needed) ...
  "%VENV%" -m pip install --no-index --find-links wheels -r requirements.txt
) else (
  "%VENV%" -m pip install -r requirements.txt
)
if not %errorlevel%==0 (
  echo [FAIL] Dependency install failed - check your internet connection.
  pause
  exit /b 1
)

if not exist "data" mkdir "data"
echo.
echo === SETUP DONE ===
echo.
echo One more manual step - load the Chrome extension:
echo   1. Open  chrome://extensions  in Chrome
echo   2. Turn on  Developer mode  (top-right)
echo   3. Click  "Load unpacked"  and pick this folder:
echo      %CD%\extension
echo.
echo Then run START.bat.
pause

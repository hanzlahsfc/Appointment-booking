@echo off
setlocal EnableDelayedExpansion
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
  pause
  exit /b 1
)
echo Using: %PY%

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not %errorlevel%==0 (
  echo [FAIL] Your Python is older than 3.11 - install a newer one and re-run.
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

:: Bundled wheels target CPython 3.15 on Windows x64 ONLY - anything else
:: needs a matching set or internet. Verify before claiming offline works.
set "USE_WHEELS=0"
if exist "wheels\" (
  "%VENV%" -c "import sys;sys.exit(0 if (sys.version_info[:2]==(3,15) and sys.maxsize>2**32) else 1)" >nul 2>nul
  if !errorlevel!==0 (
    set "USE_WHEELS=1"
  ) else (
    echo [warn] Bundled wheels are CPython 3.15/x64 - your Python differs;
    echo   falling back to online install. Run DIAGNOSE.bat if this fails.
  )
)

echo Installing dependencies (this can take a minute) ...
if "%USE_WHEELS%"=="1" (
  echo Using bundled wheels - no internet needed.
  "%VENV%" -m pip install --no-index --find-links wheels -r requirements.txt
) else (
  "%VENV%" -m pip install --upgrade pip >nul 2>&1
  "%VENV%" -m pip install -r requirements.txt
)
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo [FAIL] Dependency install failed - see the messages above.
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

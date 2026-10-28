@echo off
setlocal
cd /d "%~dp0"
set "LOG=diagnose-log.txt"
echo === Appointment Booker diagnostics === > "%LOG%"
echo %DATE% %TIME% >> "%LOG%"
echo. >> "%LOG%"

echo --- system python --- >> "%LOG%"
where py >> "%LOG%" 2>&1
py -3 --version >> "%LOG%" 2>&1
where python >> "%LOG%" 2>&1
python --version >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo --- venv interpreter --- >> "%LOG%"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" --version >> "%LOG%" 2>&1
  ".venv\Scripts\python.exe" -c "import sys;print(sys.version_info[:3], 'x64' if sys.maxsize>2**32 else 'x86')" >> "%LOG%" 2>&1
) else (
  echo NO VENV - SETUP.bat has not completed >> "%LOG%"
)
echo. >> "%LOG%"

echo --- pip check (venv) --- >> "%LOG%"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip --version >> "%LOG%" 2>&1
  ".venv\Scripts\python.exe" -m pip check >> "%LOG%" 2>&1
) else (
  echo skipped - no venv >> "%LOG%"
)
echo. >> "%LOG%"

echo --- wheels vs interpreter --- >> "%LOG%"
if exist "wheels\" (
  dir /b wheels\*.whl >> "%LOG%" 2>&1
  if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys;print('target:',sys.version_info[:2], 'x64' if sys.maxsize>2**32 else 'x86')" >> "%LOG%" 2>&1
  )
) else (
  echo no wheels folder >> "%LOG%"
)
echo. >> "%LOG%"

echo --- app.localserver import --- >> "%LOG%"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import app.localserver; print('import OK')" >> "%LOG%" 2>&1
) else (
  echo skipped - no venv >> "%LOG%"
)
echo. >> "%LOG%"

echo --- internet --- >> "%LOG%"
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri 'https://pypi.org/simple/' -TimeoutSec 5).StatusCode } catch { 'unreachable: '+$_.Exception.Message }" >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo === done === >> "%LOG%"
echo.
echo Wrote %LOG% - send it back for the next step.
pause

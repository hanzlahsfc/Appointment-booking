@echo off
setlocal
cd /d "%~dp0"
set "LOG=diagnose-log.txt"
echo === Appointment Booker diagnostics === > "%LOG%"
echo %DATE% %TIME% >> "%LOG%"
echo. >> "%LOG%"

echo --- Python checks --- >> "%LOG%"
where py >> "%LOG%" 2>&1
py -3 --version >> "%LOG%" 2>&1
where python >> "%LOG%" 2>&1
python --version >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo --- Folder contents --- >> "%LOG%"
dir >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo --- venv state --- >> "%LOG%"
dir .venv\Scripts\python.exe >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo --- Trying SETUP steps in order --- >> "%LOG%"
python -m venv .venv-test >> "%LOG%" 2>&1
if exist .venv-test\Scripts\python.exe (
  echo venv creation OK >> "%LOG%"
  rmdir /s /q .venv-test
) else (
  echo venv creation FAILED >> "%LOG%"
)
python -m pip --version >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo === done - send diagnose-log.txt back === >> "%LOG%"

echo.
echo Wrote %LOG% - open it and paste the contents back.
pause

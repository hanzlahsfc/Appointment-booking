@echo off
rem Hosted backend startup — safe to bind to Task Scheduler (onlogon) or run
rem by hand. Booking stays at whatever config.yaml says (off by default).
cd /d "%~dp0\.."
set AB_ALLOW_MOCK_PORTAL=
python -m app.hosted serve >> data\hosted-stdout.log 2>&1

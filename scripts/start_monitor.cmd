@echo off
rem Local monitor launcher (Windows) — counterpart to scripts/start_hosted.sh.
rem Provisions/reuses the "monitor" client, prints its access code on first
rem run (--issue-code rotates it), serves the dashboard on 127.0.0.1:8081.
rem Extra args pass straight through, e.g.:
rem   scripts\start_monitor.cmd --issue-code --poll-interval 30
cd /d "%~dp0\.."
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% scripts\run_local_monitor.py %*

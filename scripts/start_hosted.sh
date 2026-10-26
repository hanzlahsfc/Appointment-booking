#!/bin/bash
# Hosted backend startup (macOS/Linux) — counterpart to start_hosted.cmd.
# Provisions/reuses the "monitor" client, prints its access code on first
# run (--issue-code rotates it), serves the dashboard on 127.0.0.1:8081.
# Extra args pass straight through, e.g.:
#   bash scripts/start_hosted.sh --issue-code --poll-interval 30
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" scripts/run_local_monitor.py "$@"

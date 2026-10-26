# Current handoff

Updated 2026-10-26. Active development branch: `hosted-client`.

Read [project context](docs/PROJECT-CONTEXT.md) and [startup guide](docs/AGENT-STARTUP.md) first.

## Current state

- Local dashboard and isolated visible browser launched with `python scripts/run_local_monitor.py`.
- Default monitoring-only; `--arm-booking` requires configured picker/slot/reserve selectors.
- Local launcher uses `data/monitor-hosted.db`, `data/monitor-browsers/`, and the configured slot database.
- 147 backend tests passed after fixes for legacy database inserts, CAPTCHA iframe access, cross-origin fetch credentials, login refresh interruptions and all-unparseable slots.
- Normal restart preserves client dashboard sessions. `--issue-code` rotates the selected client's code and revokes sessions.

## Outstanding

1. Verify real portal login and selectors. Latest local runs experienced HTTP/2 connection errors; selectors are absent.
2. Confirm actual reservation/hold semantics under supervised authorization. No real successful reservation is established.
3. Deploy the client dashboard and secure backend tunnel; test externally. No deployment URL is established.
4. Recheck manual-control concurrency, new-client monitor activation, hosted recovery commands and load before multi-client operation.
5. Background-tab audio can be suspended; persistent visual alerts and foreground testing remain necessary.

Do not guess selectors, bypass portal controls, clear unknown reservations blindly, or treat blocked reads as no appointments. Do not commit access codes or runtime data.

Historical handoff is retained at [docs/archive/HANDOFF-2026-10-26.md](docs/archive/HANDOFF-2026-10-26.md); commands and test counts there may be stale.

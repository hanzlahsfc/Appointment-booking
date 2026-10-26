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
   **2026-10-26 real-access evidence:** `https://pk-gr.gvcworld.eu/en/schedule-appointment`
   loads (200, real page). `https://pk-gr-services.gvcworld.eu/login/appointments/en`
   fails as `ERR_HTTP2_PROTOCOL_ERROR` in plain visible Chromium AND returns the
   958-byte Incapsula shell over `curl --http1.1`. Conclusion: this egress IP is
   hard-flagged on the services domain only — an IP-reputation block, not an app
   bug or a fixable HTTP/2 quirk. Operator action: retry from a different
   legitimate network (home broadband/hotspot) or a residential proxy, then
   re-run the picker observation. `--disable-http2` added to hosted browser
   launch args as transport robustness. CAPTCHA iframe fix verified present in
   code (`CHALLENGE_FRAME_HOSTS`, subframe-only allowance).
2. Confirm actual reservation/hold semantics under supervised authorization. No real successful reservation is established.
3. Deploy the client dashboard and secure backend tunnel; test externally. No deployment URL is established.
4. Recheck manual-control concurrency, new-client monitor activation, hosted recovery commands and load before multi-client operation.
5. Background-tab audio can be suspended; persistent visual alerts and foreground testing remain necessary.

Do not guess selectors, bypass portal controls, clear unknown reservations blindly, or treat blocked reads as no appointments. Do not commit access codes or runtime data.

Historical handoff is retained at [docs/archive/HANDOFF-2026-10-26.md](docs/archive/HANDOFF-2026-10-26.md); commands and test counts there may be stale.

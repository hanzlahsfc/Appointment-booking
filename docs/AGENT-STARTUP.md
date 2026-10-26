# Agent runbook: start, connect, and adapt the hosted crawler

Read PROJECT-CONTEXT.md first. This is the active operator runbook as of 2026-10-26. Real Greece selectors and reservation hold semantics remain unverified. Passing mock tests does not prove real booking.

## Start on this Windows host

PowerShell:

```powershell
cd C:\Users\Hanzlah\Desktop\appointment-booker
python -m pip install -r requirements.txt
python -m playwright install chromium
python scripts/run_local_monitor.py --issue-code
```

Open http://127.0.0.1:8081 and enter the code printed in the terminal. Codes are private: never commit them or paste them into public diagnostics. Restart without --issue-code to preserve existing dashboard sessions. This launcher uses data/monitor-hosted.db and data/monitor-browsers, separate from the original demo database. Portal browser profiles are isolated from personal Chrome.

Default is MONITOR ONLY, visible Chromium, 15 seconds between completed checks. Use Take control before manual login/CAPTCHA; release control only after ready. Keep the dashboard visible and click Enable sound and Test sound. Do not interpret awaiting_login, configuration errors, blocked pages, or parse failures as zero appointments.

If port 8081 is busy, inspect the owner before stopping anything:

```powershell
Get-NetTCPConnection -LocalPort 8081 -State Listen | Select-Object OwningProcess
```

Stop the known crawler with Ctrl+C in its terminal. Never kill unrelated browsers/processes or delete profiles/databases to resolve a port conflict. Restart after changing provisioned clients/selectors: monitors currently load their settings at startup.

## Configure real portal selectors

Use docs/booking-selectors.example.json as a SHAPE example, not verified real selectors. Observe the actual page under manual control. Configure vac_select, category_select, slot_row, reserve_button, and the confirmation_marker needed to verify final completion. Category/location must be actual option values or exact visible labels; friendly labels may not match portal values. Store observed configuration locally under recon/ (ignored by Git).

```powershell
python scripts/run_local_monitor.py --selectors-file recon/booking-selectors.json
```

Compare detected slots to the visible page with booking off. Missing selectors intentionally prevent successful monitoring; do not substitute guessed selectors to make status green. Unsupported date formats must fail visibly rather than deactivate existing slots.

## Assisted reservation (only after validation and authorization)

```powershell
python scripts/run_local_monitor.py --selectors-file recon/booking-selectors.json --arm-booking
```

This explicitly enables reserve clicks for the selected client, with no automatic final submission. The launcher refuses arming when required selectors are missing. The client must keep the dashboard open for its alarm and finish manually. A reserve click is not proof of a held slot; observe actual portal state. Ambiguous outcomes park automation. Verify completion marker/reference before marking booked.

Unresolved bookings live in the configured slot database (currently data/slot_state.db), scoped by profile_name client:<client>. Legacy main.py recovery commands must NOT be used blindly: list/verify the correct row/profile first. Do not clear needs_human/unknown rows just to resume polling; verify completion or abandonment on the portal. Back up local state before operator recovery. Never expose database editing to remote clients.

## Another website or another client

The exact other website has not been supplied. Do not guess its URL. Ask the operator for the official URL and intended category/centre, then inspect its normal booking process and permitted use.

```powershell
python scripts/run_local_monitor.py --client CLIENT_ID --url 'https://OFFICIAL-HOST/appointments' --origin 'https://OFFICIAL-HOST' --category 'ACTUAL_OPTION' --location 'ACTUAL_OPTION' --selectors-file recon/OTHER-SITE.json --issue-code
```

Replace ALL placeholders. The site driver currently assumes HTML selects and textual date/time rows. A different widget, API, OTP, payment, queue, or multi-step picker requires a tested adapter; selectors alone may be insufficient. Add mock fixtures/tests before live reservation. Never reuse one client's browser profile or tokens for another.

Portal top-level navigation is restricted to configured origins. HTTPS CAPTCHA documents from explicit providers may load as subframes only; this does not allow clients to navigate the top page to those providers. Do not disable CAPTCHA, impersonate another applicant, brute-force endpoints or remove access restrictions to get availability.

## Site inaccessible

A public information page loading does not establish booking health. HTTP 200 can be an Imperva incident/challenge page. Check the isolated browser itself. Complete normal human CAPTCHA if offered, and pause navigation while doing so. ERR_HTTP2_PROTOCOL_ERROR/timeouts or Access denied are failures, not zero slots. Respect throttling and wait/back off. If necessary, the operator can test another legitimate network (for example their own hotspot). No alternate authorized booking endpoint has been established; use the official entry point and support channel. Do not claim a site-wide outage from one blocked connection.

Official Greece entry: https://pk-gr.gvcworld.eu/en/schedule-appointment
Observed linked booking URL: https://pk-gr-services.gvcworld.eu/login/appointments/en

## Public dashboard and backend connection

The dashboard is currently local. Existing Vercel authentication was observed on this host, but no successful deployment URL is recorded. Before deployment verify all client-dashboard files contain no credentials.

1. Run the local server with safe settings above.
2. Configure a stable authenticated deployment/tunnel using deploy/cloudflared.example.yml. A downloaded tools/cloudflared.exe is a local ignored artifact, not a bundled requirement. Follow the provider's current setup instructions and keep tunnel credentials out of Git.
3. Set client-dashboard/config.js window.AB_API_BASE to the backend HTTPS origin, never localhost for a remote client.
4. Deploy the static directory from its own folder:

```powershell
cd client-dashboard
vercel --prod
```

5. Add the exact returned dashboard origin under hosted.dashboard_origins in config.yaml and restart the backend. Do not allow arbitrary origins. Frontend cross-origin API requests use Bearer tokens without cookies.
6. Test access-code login, preflight/CORS, WSS screencast, control, offline/reconnect and alarm from a different network. Publishing static files alone does not connect a remote client to the laptop.
7. Never expose raw Chrome debug ports or the host desktop. The laptop must remain awake and online. Admin GUI/VPS/proxy migration are future scope.

## Verification and handoff

```powershell
python -m pytest tests -q
node --check client-dashboard/app.js
python scripts/run_local_monitor.py --help
```

Write actual results, branch/commit, active process/start command, configured client ID, blockers and whether real selectors are verified. Do not include access codes, cookies or raw network captures. The code changes accompanying this runbook fix legacy account inserts, CAPTCHA frame blocking, cross-origin fetch credentials, repeated login navigation, and all-unparseable slot handling. HTTP/2 transport problems and true reservation semantics are still external/live-verification concerns.

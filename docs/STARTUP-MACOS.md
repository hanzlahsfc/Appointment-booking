# Running the hosted server on macOS

Operator runbook for this Mac. The Windows version lives in
`docs/AGENT-STARTUP.md`; the same rules apply (no selectors = no real
availability data, booking stays off until authorized).

## One-time setup

```bash
cd "/Users/apple/Desktop/Devin Next/Appointment-booking"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Daily start

```bash
bash scripts/start_hosted.sh
```

- Prints the dashboard URL (http://127.0.0.1:8081) and, on first run, the
  client's one-time access code.
- Launches one isolated, visible Chromium (`data/monitor-browsers/`). It is
  the remote browser the client drives — leave it alone.
- Monitoring-only by default; `--arm-booking` stays blocked until real
  selectors exist.

Rotate the code / revoke dashboard sessions:

```bash
bash scripts/start_hosted.sh --issue-code
```

## Stopping

Ctrl+C in the terminal running it. Do not `kill -9` — that orphans the
managed Chromium and the next polls loop `TargetClosedError` until restart.
If that happens, just restart; the browser relaunches on serve start.

## Public client access (Vercel + tunnel)

Dashboard on Vercel: https://gvcw-slot-monitor.vercel.app

The dashboard calls the backend over `window.AB_API_BASE` in
`client-dashboard/config.js`, currently a Cloudflare quick tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8081
```

Quick-tunnel URLs are ephemeral. When it restarts with a new hostname:

1. Update `window.AB_API_BASE` in `client-dashboard/config.js`.
2. Redeploy: `cd client-dashboard && npx vercel deploy --prod --yes`.
3. Keep the dashboard's exact origin in `hosted.dashboard_origins` in
   `config.yaml` (CORS whitelist) — already set for the Vercel domain.

Bearer-token auth means no cookies cross origins; CORS only needs
`Authorization`/`Content-Type`.

## Client flow

1. Client opens the Vercel link, enters their access code.
2. They see a live screencast of their isolated browser on this Mac.
3. **Take control** → sign in to the GVCW portal, solve CAPTCHA by hand →
   release control. Manual control idles back to automation after
   `hosted.manual_idle_seconds` (15 min).
4. Alarm + banner fire client-side when a matching slot is claimed.
   Clients click "I completed the booking" only after the portal confirms.

## Provisioning more clients

```bash
.venv/bin/python -m app.hosted add-client NAME \
    --booking-url "https://pk-gr-services.gvcworld.eu/login/appointments" \
    --origin "https://pk-gr-services.gvcworld.eu" \
    --category national_d_employment --location lahore
.venv/bin/python -m app.hosted list-clients
.venv/bin/python -m app.hosted issue-code NAME     # rotate + revoke sessions
.venv/bin/python -m app.hosted deactivate NAME
```

Restart the server after adding clients — monitors load config at startup.

## State files

| Thing | Path |
|---|---|
| client accounts + sessions | `data/monitor-hosted.db` |
| slot/booking records | `data/slot_state.db` |
| per-client browser profile | `data/monitor-browsers/client-<id>/` |
| recon captures (selectors source) | `recon/` (git-ignored) |

## Known blockers (as of 2026-10-26)

- **Selectors**: real picker/reserve selectors are unobserved. Availability
  monitoring reports `paused: configuration` until `recon/` captures land and
  `--selectors-file` is passed.
- **Egress**: this IP is flagged on the services domain (HTTP/2 resets /
  Incapsula hCaptcha). A human sign-in in the recon window or a different
  egress is required; see `docs/OPERATIONS.md` §6.
- **Booking**: off. `--arm-booking` requires `--selectors-file` and operator
  authorization.

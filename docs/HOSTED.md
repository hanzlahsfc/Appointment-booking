# Hosted clients — remote portal browsers on the local host

Status: **first milestone built and demonstrated locally.** Each provisioned
client drives one isolated browser on this machine through a live screencast;
automation runs in the same browser and pauses while the client is in control.

```
 client browser (dashboard SPA)                    host laptop
 ┌───────────────────────────┐   HTTPS + WSS    ┌──────────────────────────────┐
 │ login -> live screen img  │ ───────────────▶ │ FastAPI  python -m app.hosted│
 │ clicks / keys / control   │ ◀─────────────── │  ├─ HostedStore (accounts)   │
 └───────────────────────────┘   frames+events  │  ├─ BrowserHostManager      │
                                                │  │   per-client Chromium    │
                                                │  │   profile + nav guard    │
                                                │  ├─ HostedMonitor per client│
                                                │  └─ booking_log / slots DB │
                                                └──────────────────────────────┘
```

## What a client can and cannot do

- Sees a JPEG screencast of **their own** browser; input is a whitelisted set
  of mouse/keyboard events dispatched through Playwright (controlled CDP
  `Input.*`), rate-limited per connection.
- Input is accepted only while the client holds **manual control**
  (Take control button); automation pauses while a client holds it, and an
  idle lease (`hosted.manual_idle_seconds`, default 15 min) hands control back.
- Navigation is locked to the client's `allowed_origins` (route interception
  aborts the request before it leaves; a watchdog catches `data:`/`javascript:`
  navigations and steers back to the portal page). Blocked attempts surface to
  the client as `nav_blocked` events.
- No desktop, no filesystem, no downloads, no file picker, no popups, no
  JavaScript execution, no raw CDP. There is no endpoint that evaluates
  expressions — input is the only thing that flows in.
- Clients are provisioned by CLI only; there is no signup surface.

## Provisioning (operator)

```bat
:: mock-portal test client (requires hosted.allow_mock_portal or AB_ALLOW_MOCK_PORTAL=1)
python -m app.hosted add-client alice --name "Alice" --password "long pass" --preset mock

:: real client — fill in the real portal origin/selectors after recon
python -m app.hosted add-client alice --name "Alice" --password "long pass" ^
    --booking-url "https://pk-gr-services.gvcworld.eu/login/appointments" ^
    --origin "https://pk-gr-services.gvcworld.eu" ^
    --category national_d_employment --location lahore ^
    --selectors-file recon/booking-selectors.json --auto-book

python -m app.hosted list-clients
python -m app.hosted passwd alice --password "new pass"
python -m app.hosted deactivate alice
```

## Running

```bat
python -m app.hosted serve                 :: dashboard on http://127.0.0.1:8081
python -m app.hosted serve --booking-mode assist --booking-dry-run
```

Booking stays governed by the existing rules: `booking.mode` must be `assist`
(`auto` is never offered to hosted mode) AND the client row's `auto_book` flag
must be set. `booking.dry_run` defaults to true — no reserve click without an
explicit `--no-booking-dry-run`.

### Local demo on the fake portal (TEST ONLY)

```bat
set AB_ALLOW_MOCK_PORTAL=1
python -m app.hosted serve --with-mock-portal --booking-mode assist --no-booking-dry-run --poll-interval 5

:: another terminal
set AB_ALLOW_MOCK_PORTAL=1
python -m app.hosted add-client demo --password demo-pass-123 --preset mock
python scripts\demo_hosted.py --username demo --password demo-pass-123 --seed "2026-12-15 09:00"
```

`demo_hosted.py` is a real remote client (HTTP + WebSocket only): it logs in,
takes control, signs in to the portal by keyboard, releases, watches the
monitor claim a seeded slot in assist mode, takes control again, and finishes
the applicant form. Or do the same thing by hand at http://127.0.0.1:8081.

## State model

| Thing | Where it lives |
|---|---|
| client accounts + sessions | `data/hosted.db` |
| slot/booking records | `data/slot_state.db` (shared with the crawler) |
| per-client browser profile | `data/hosted-browsers/client-<id>/` |

An unresolved `booking_log` row (`needs_human` / `aborted_unknown_state` /
in-flight) parks that client's monitor until it is resolved — same semantics
as the crawler, and it survives restarts. Resolve with
`python main.py --resolve-booking <id>` or `--list-bookings` to inspect.

## Windows startup

No service wrapper yet — run in a terminal, or via Task Scheduler at logon:

```bat
schtasks /create /tn "AppointmentBookerHosted" /sc onlogon ^
  /tr "cmd /c cd /d C:\Users\Hanzlah\Desktop\appointment-booker && python -m app.hosted serve >> data\hosted.log 2>&1"
```

## Not done yet (next milestones)

- Vercel-hosted dashboard build + Cloudflare Tunnel in front of this server
  (the SPA already separates API origin via `window.AB_API_BASE`; cookie is
  SameSite=Lax, so the tunnel domain will need SameSite=None + Secure)
- Admin dashboard; clients provisioned at runtime are only picked up on
  `serve` start
- VPS deployment; one Playwright-managed Chromium per client already works —
  containers/noVNC only if scale demands it
- Residential proxies for the real portal's WAF (tested blocked from this IP)

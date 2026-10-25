# HANDOFF — hosted-client branch

State as of 2026-10-26. Branch: `hosted-client` (forked from `extension-only` @ 47a6db2).

## Commits

- `a94529a` — hosted foundation: HostedConfig, accounts/sessions store
  (`data/hosted.db`), BrowserHostManager, HostedMonitor, FastAPI server,
  client-dashboard SPA, CLI, fake-portal admin API, 11 unit tests
- `aec9166` — e2e proof: full client flow on real browsers + nav-guard tests
- pending — this file + docs/HOSTED.md + demo script + config block

## What works (verified, not claimed)

- `python -m pytest tests/` → **142 passed** (128 pre-existing + 14 new)
- Live demo on the real server (run 2026-10-26 ~03:17 PKT):
  provisioned `demo` client → HTTP login → WS screencast → monitor hit
  portal login → client took control and signed in by forwarded keys →
  released → monitor polled → seeded slot → assist claim (`needs_human`,
  booking_log id=1) → client took control, finished form → portal returned
  confirmation `GVCW-5DE1A7`. Server log showed `HOSTED OK` polls and the
  alarm fired.
- Dashboard SPA verified in a real browser (screenshot `data/hosted-dash.png`).
- Two-client isolation proven in `tests/test_hosted_e2e.py`
  (`test_hosted_client_flow_end_to_end`): Bob's token reads only Bob's
  records/stream/browser; anonymous WS is refused with 4401; bad password
  401; missing auth 401.
- Navigation guard proven (`test_nav_guard_blocks_disallowed_navigation`):
  disallowed navigation aborted + tab steered back; mock origin refused when
  `allow_mock_portal` is off.

## Known limitations / unfinished

- Monitors bind clients at `serve` start — a client provisioned while the
  server runs gets a browser (on WS connect) but **no monitor** until restart.
- Manual-control pause is checked between steps, not preempted mid-step:
  a poll that already started may finish its navigation before pausing.
- Screencast is JPEG-over-JSON — fine locally; move to binary frames if
  latency over Cloudflare Tunnel is poor.
- Headless Chromium only so far; the real portal likely needs `headless:
  false` + `channel="chrome"` and WAF work (real egress still blocked).
- No Vercel deploy, no Cloudflare Tunnel, no admin UI, no persistence of
  "control" state across restarts (resets to automation — intentional).

## Exact next steps

1. `client-dashboard/` → Vercel static deploy; set `window.AB_API_BASE` or
   serve from the tunnel origin; add SameSite=None cookie path for cross-site.
2. `cloudflared tunnel` in front of `http://127.0.0.1:8081`; restrict via
   Cloudflare Access or keep token-only; document Windows autostart.
3. Offline/reconnect polish in the SPA (skeleton exists: 2 s reconnect).
4. Real-portal provisioning path: recon → selectors JSON → `add-client`
   without `--preset`.
5. Two-client soak on the ThinkPad (RAM is fine: one Chromium ≈ 300 MB).

## Startup commands

```bat
cd C:\Users\Hanzlah\Desktop\appointment-booker

:: hosted server (production-ish defaults; booking off)
python -m app.hosted serve

:: local test rig (mock portal, assist mode, fast polls)
set AB_ALLOW_MOCK_PORTAL=1
python -m app.hosted serve --with-mock-portal --booking-mode assist --no-booking-dry-run --poll-interval 5

:: provision / inspect clients
python -m app.hosted add-client USER --password PASS --preset mock
python -m app.hosted list-clients

:: resolve a held booking after checking the portal
python main.py --list-bookings
python main.py --resolve-booking <id>

:: e2e demo driver
python scripts\demo_hosted.py --username demo --password demo-pass-123 --seed "2026-12-15 09:00"
```

## Demo leftovers in data/ (safe to wipe)

`data/hosted.db` has the `demo` client; `data/slot_state.db` has an
unresolved `needs_human` booking for `client:demo` (id=1) — resolve or
delete both DBs before re-demoing. `data/hosted-browsers/` holds the demo
browser profile.

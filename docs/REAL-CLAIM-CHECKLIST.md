# Supervised real claim — exact checklist

One authorized attempt at a REAL reservation on the GVCW portal through a
hosted client. Nothing here auto-submits; the human finishes the form.

## Preconditions — verify before starting

- [ ] **Egress not WAF-blocked.** `python -c "import asyncio; from app.preflight import check_reachability; from app.session import LOGIN_URL; r=asyncio.run(check_reachability(LOGIN_URL)); print(r.ok, r.blocked, r.detail)"`
      must print `ok=True`. As of 2026-10-26 this host gets the Incapsula
      shell (`blocked=True`) — do not proceed until the egress changes
      (residential IP / proxy / different network).
- [ ] **Recon done.** `recon/booking-selectors.json` exists and was captured
      from the real signed-in portal (run `python recon.py --profile ...`
      from a clean network). Without it the driver refuses to act.
- [ ] Client row provisioned with the REAL portal origin:
      `python -m app.hosted add-client <name> --booking-url https://pk-gr-services.gvcworld.eu/login/appointments --origin https://pk-gr-services.gvcworld.eu --category <cat> --location <loc> --selectors-file recon/booking-selectors.json --auto-book`
      → save the printed access code (shown once).
- [ ] `allow_mock_portal` is **false** and no `AB_ALLOW_MOCK_PORTAL` env is set.
- [ ] `booking.mode` will be `assist` and `booking.dry_run` false **only for
      this authorized run** via CLI flags — config defaults stay off/dry.

## Startup (one command)

```bat
scripts\start_hosted.cmd
```
(equivalent: `python -m app.hosted serve`; add the flags below for the claim)

For the supervised claim run instead:
```bat
python -m app.hosted serve --booking-mode assist --no-booking-dry-run
```

## The claim

1. Client opens the dashboard, signs in with the access code, clicks
   **Enable sound**.
2. Client takes control, signs in to the portal (reCAPTCHA is theirs),
   releases control.
3. Monitor shows `ok` — check the metrics line (last check / latency /
   effective interval / throttle).
4. When a matching slot appears the agent claims it → `needs_human` →
   client alarm sounds + banner persists → client takes control and
   **finishes the form by hand**. Nothing else submits.
5. After the portal's confirmation page shows, the client presses
   **I completed the booking** (or releases control — next poll checks).
   The row flips `needs_human` → `booked` with the confirmation ref, and
   the profile cannot book again.

## If it goes wrong

- Reserve clicked but outcome unknown → row stays
  `aborted_unknown_state`/`needs_human`: polling parks, DB-backed, survives
  restart. Check the portal manually, then `python main.py --list-bookings`
  / `--resolve-booking <id>`.
- `holding`/`manual_control` throttle states mean automation is parked —
  check the dashboard status line for the reason.

## Explicitly NOT done

- No real reservation was made during development. The "live demo" was the
  loopback mock portal only.
- `auto` mode is not offered to hosted clients; `--booking-mode` accepts
  only `off|assist` on the hosted server.

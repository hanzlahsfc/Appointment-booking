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

## Bridge recon (operator channel into the signed-in session)

`app/cdp_bridge.py` adds a safety-gated `BridgePage` over the devtools-
extension HTTP bridge (:1232) — the duck-typed subset `GvcwSiteDriver`
needs, driving YOUR authenticated tab. Hard rules: one explicit tab id
verified before every op, URL origin allowlist enforced continuously,
BridgeSessionLost on login form/401, BridgeChallenge on captcha,
BridgeDisconnected when the tab/host dies, read-only unless allow_writes.

`python -m app.hosted bridge-recon --tab N --origin ORIGIN [--category 26 --target-date DD/MM/YYYY]`
= selector assertions + ONE real Search + redacted evidence in data/recon/.
Docs: docs/BRIDGE-RECON.md. Verified vs faithful mock over real HTTP
transport (tests/test_bridge.py) — not yet run against live portal.
`app/bridge.py` (the project's own WS bridge + legacy BridgeSiteDriver)
is untouched and separate.

## WINDOWS-APP MILESTONE (active task — resume here)

Goal: client-installed Windows app — bundled Chrome extension + local
Python server/dashboard + sequential applicant queue. Local-first only
(no Vercel/tunnel/VPS/multi-client). Reuse existing pieces, do NOT build
competing impls.

Existing assets to build on:
- `app/bridge.py` — project's own WS bridge (BridgeClient/BridgeHub,
  current_tab(), evaluate/command, origin+token checks, tab claiming).
  `site.py` has `BridgeSiteDriver` (legacy select-driver). tests/test_bridge.py = 29 tests.
- `app/cdp_bridge.py` — NEW duck-typed Playwright page over devtools-extension
  HTTP :1232 (explicit tab, per-op origin gate, session/challenge stops,
  read-only default). Used by bridge-recon/bridge-monitor. tests/test_cdp_bridge.py.
- `app/gvcw.py` — GvcwSiteDriver (search-driven real portal: #type/#datefrom/
  #btn-search → PUT /api/v1/periodslot/slots → .appointment_slot grid).
- `app/bridge_monitor.py`, `app/bridge_recon.py`, `python -m app.hosted
  bridge-recon|bridge-monitor`.
- `extension/` folder + `appointment-booker-extension.zip` (project bridge
  extension — verify which transport it uses).
- Hosted server/dashboard (app/hosted/*, client-dashboard/*) — reusable
  UI primitives (alarm, bookings feed, input stream) but the new app is
  SINGLE-CLIENT local; do NOT expose the bridge to the web page.

Commits so far this task:
- dc588ac restored 29 WS-bridge tests; my HTTP tests → test_cdp_bridge.py (31/31 pass)
- ca2a6cc cdp_bridge fixes + bridge-monitor

STILL TO DO (in order):
1. Search freshness: gvcw._search must not accept a stale #result/#resultMessage
   from a previous date — record pre-search marker state and require it to
   CHANGE (or wait for the loader cycle to complete). Same for list_slots.
2. Queue model: ordered applicants persisted in SQLite (add queue table;
   states: queued/monitoring/selected-awaiting-human/submitting/booked/
   failed/outcome-unknown; identity dedup WITHOUT storing raw passport).
3. Local app server: pairing-auth localhost API + dashboard page
   (queue CRUD, category/date-range config, start/pause/stop monitor,
   current applicant/date/latency/last-check/errors, alarm with
   Enable/Test/Acknowledge, OTP request+entry, Confirm-and-book).
4. Extension pairing: bundled extension connects selected tab to
   localhost (origin check, clear disconnect state, operator picks tab).
   Prefer the existing WS bridge (app/bridge.py) over devtools-extension.
5. Monitor loop per UX steps 7-11: detect→select→prefill→alarm→
   manual OTP/captcha→Confirm-and-book→verify /result/{ref}→advance to
   next applicant. Ambiguous submit → park queue for review, never retry.
6. Windows package: SETUP.bat (venv, dep check, extension-load
   instructions — no silent install claims), START.bat (port check,
   no dup server, opens dashboard), CLIENT-README.md, packaging script
   producing a ZIP excluding secrets/data/profiles/.git.
7. Tests: sequential 2-applicant flow, restart persistence, stale search,
   disabled/empty/malformed grids, lost slot, wrong OTP, captcha pending,
   ambiguous submit, disconnect/wrong-origin, pairing auth — all on the
   faithful mock. NO real OTP/booking.
8. Verify centre display (account-bound, read-only) — never promise
   "any centre".

Real-portal state: live /add page verified via devtools-extension bridge
(tab must be signed in). Search requires DOB+passport filled (validator);
fill_applicant runs BEFORE _search. Centre = account-bound (vac=138).

## Windows-app milestone — status

Done + tested (154 tests green):
- `app/queue.py` — ApplicantQueue: position-ordered, SQLite-persisted,
  statuses queued/monitoring/selected-awaiting-human/submitting/booked/
  failed/outcome_unknown, sha256(surname+passport) identity dedup, `get()`.
- `app/localapp.py` — LocalMonitor: queue-driven detect→select→prefill→
  handoff; OTP/Confirm are operator actions; verified /result/{ref} marks
  booked and advances; ambiguous → outcome_unknown (parked, never retried).
- `app/localserver.py` — local FastAPI dashboard + API (token pairing),
  hosts `ws://127.0.0.1:8080/bridge` for the bundled extension.
- `app/ws_page.py` — Playwright-shaped page over the project's own WS
  bridge (extension) — the preferred transport, no devtools-extension.
- `app/cdp_bridge.py` — same surface over devtools-extension HTTP :1232
  (operator/testing path; kept, not wired into the app).
- `tests/test_localapp.py` — sequential 2-applicant booking, restart
  persistence, ambiguous-submit parking, duplicate-identity rejection (3).
- SETUP.bat / START.bat / CLIENT-README.md / package_app.py (ZIP builder).

Search freshness: `_search` hooks fetch+XHR for the periodslot PUT and
waits on ITS completion — stale markers can't pass; un-fired searches
(validator) error. reCAPTCHA on /add is normal — challenge = Incapsula +
no booking form. `_daterange` is inclusive (same-day range = one date).

Still open: live smoke of the WS extension pairing + a first supervised
real booking; SETUP/START only lightly exercised on this machine
(Python 3.12); no real-Windows extract+run e2e yet.

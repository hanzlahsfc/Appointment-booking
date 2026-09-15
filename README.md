# GVCW Appointment Slot Monitor

24/7 crawler that watches the Greece visa appointment portal (GVCW Pakistan) and emails an alert the moment a **new work-permit / premiere-launch appointment slot** opens. Booking stays manual — **no auto-booking**.

- Monitors per applicant profile (multiple profiles supported)
- Passes the Imperva/Incapsula WAF by driving a real Chromium browser
- Diffs each poll vs. SQLite state; alerts only on slots never seen before
- Web dashboard (`http://<host>:8080`) shows live worker status, slots, and the alert feed
- Demo mode (`--demo`) fabricates realistic slots so you can screenshot/pitch without real credentials
- Runs on a ~$5/mo Hetzner VPS (Docker or systemd)

---

## Quick start

### 1. Install

```bash
cd gvcw-crawler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

### 2. Configure

- `config.yaml` — profiles, poll interval, categories, VAC locations, notification recipients
- `.env` — `RESEND_API_KEY` (copy from `.env.example`)

### 3. Credentials (one file per profile)

The crawler reads login credentials from `data/creds/<slug>.json`, where `<slug>`
is the profile name slugged (`"Ahmed - Work Permit"` → `Ahmed_-_Work_Permit`):

```bash
mkdir -p data/creds
echo '{"username":"you@example.com","password":"YOUR_PASSWORD"}' \
  > data/creds/Ahmed_-_Work_Permit.json
chmod 600 data/creds/Ahmed_-_Work_Permit.json
```

The first run logs in once and persists the browser session to `state/<slug>.json`,
so later runs skip the login step.

### 4a. Demo mode (no credentials, no site) — for showcasing

```bash
.venv/bin/python main.py --demo --port 8080
# open http://127.0.0.1:8080
```

Populates the dashboard with fabricated work-permit / premiere-launch slots and a
simulated alert feed. Perfect for client screenshots.

### 4b. Real mode

```bash
.venv/bin/python main.py
# dashboard on http://127.0.0.1:8080, JSON at /api/status
```

First dry-run safety check:

```bash
# in config.yaml set app.dry_run: true  (or run:)
.venv/bin/python -c "import yaml,os; c=yaml.safe_load(open('config.yaml')); c['app']['dry_run']=True; yaml.safe_dump(c,open('/tmp/cfg.yaml','w')); os.rename('/tmp/cfg.yaml','config.yaml')"
.venv/bin/python main.py     # logs what it WOULD alert, sends no email
```

⚠️ **Reachability check required first** — see §"The WAF/IP block" below.

---

## Project layout

```
gvcw-crawler/
├── main.py                  # entry point: scheduler + dashboard server
├── requirements.txt
├── config.yaml              # profiles, intervals, recipients
├── .env.example             # secrets template (never commit .env)
├── .gitignore
├── app/
│   ├── config.py            # config/env loading + validation
│   ├── browser.py           # Playwright context factory + Incapsula handler
│   ├── session.py           # login + storage_state persistence, expiry detection
│   ├── crawler.py           # CrawlerWorker: per-profile poll loop (+ demo mode)
│   ├── diff.py              # slot diffing rules
│   ├── store.py             # SQLite: slot state + alert log
│   ├── notifier.py          # Resend email client (retry / dry-run)
│   ├── util.py              # PKT timezone helper, poll jitter, slugify
│   ├── dashboard.py         # in-process live state → dashboard
│   ├── web.py               # FastAPI: serves UI + /api/status
│   └── static/index.html    # the web dashboard
├── tests/
│   ├── test_diff.py         # diffing acceptance clauses
│   └── test_store.py        # store/diffing integration + restart
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── gvcw-crawler.service # systemd unit (alternative)
├── data/                    # SQLite DB (data/slot_state.db), creds/ (never commit)
└── state/                   # persisted browser sessions per profile
```

---

## CLI reference

```bash
python main.py                # production crawl
python main.py --demo         # demo mode (fake slots, populated dashboard)
python main.py --port 9090    # dashboard port (default 8080)
python main.py --host 0.0.0.0 # bind (default loopback only)
```

---

## Configuration (`config.yaml`)

```yaml
app:
  poll_interval_seconds: 300   # seconds between polls (5 min)
  jitter_seconds: 30           # +/- random delay to avoid being a robot
  concurrency: 4               # max simultaneous browser contexts
  timezone: "Asia/Karachi"     # all timestamps/states in PKT
  dry_run: false               # true = check + log, no emails, DB untouched
  headless: true               # set false during the recon pass

profiles:
  - name: "Ahmed - Work Permit"      # free-text; drives file slugs
    email: "alerts@gvcwclient.example"
    category: "national_d_employment"  # friendly label: "Work Permit"
    location: "lahore"               # isl | lah | ...
    booking_url: "https://pk-gr-services.gvcworld.eu/login/appointments"

notifications:
  resend_api_key_env: "RESEND_API_KEY"  # env var holding the key
  from: "GVCW Slot Alert <oncall@yourdomain.com>"
  to: ["client@example.com"]            # fallback recipients per profile
```

**Category → dashboard label map**

| category key | Shown as |
|---|---|
| `national_d_employment` | Work Permit |
| `national_d_seasonal` | Premiere Launch |
| `national_d` | National D Visa |
| `schengen_c` | Schengen C (no longer used in this build) |
| `attestation` | Attestation |

Secrets live only in `.env`: `RESEND_API_KEY`, optional `RESEND_FROM`.

---

## Verification status (as of build)

| Acceptance (§10) | Status |
|---|---|
| Diffing clauses (same/no, new/yes, removed/no, re-add/yes-if-unalerted, restart/no-dup) | ✅ 11 unit tests pass |
| Email: Resend request shape + retry + failure handling | ✅ verified against mocked API |
| Demo dashboard population | ✅ verified end-to-end |
| Compile/import of all modules | ✅ verified |
| **Live dry-run vs. what a human sees** | ⛔ blocked — see below |
| **Real test email to client** | ⛔ needs live credentials + clean IP |

---

## The WAF/IP block (read before commissioning)

The member portal is guarded by Imperva/Incapsula. From the network used during
development, **every** connection is hard-blocked at the **IP level**:

```
HTTP 403 → incident shell + visid_incap_* / incap_ses_* cookies
challenge iframe: "Access denied, Error 17 — this request was blocked
                   by our security service. Your IP: 185..."
```

This was reproduced with plain `curl`, Playwright headless shell, Playwright
full-Chromium headless, Playwright **headful** Chromium, **and a real Google
Chrome window**. Since real Chrome is also blocked, the block is caused by the
**egress IP reputation** (VPN/datacenter ranges like `185.100.233.x` are
hard-flagged) — **not** by the crawler code or automation detection.

Consequences:

1. `main.py` will run fine, but every real-mode poll will fail to reach the
   login/slot page from a flagged IP.
2. Before paying for a VPS, verify its IP can load the portal (see
   `docs/OPERATIONS.md` → "WAF reachability check").
3. Fallbacks if a clean IP can't be obtained: residential proxy, or hosting on
   the client's home broadband.

---

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the system works end to end
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — infrastructure, Docker, systemd, VPS
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — running, monitoring, troubleshooting, recon

## Recon notes (§11)

Login page: `https://pk-gr-services.gvcworld.eu/login/PK/en`
Post-login redirect, slot-fetch endpoint, slot-picker selectors: **TBD** — the
mandatory recon pass (§2.4) must be completed from a WAF-clean network with real
credentials. Placeholders live in `app/crawler.py` (`SLOT_SELECTOR_HINT`,
`_parse_slot_label`, `session.LOGGED_IN_EXPECT`).
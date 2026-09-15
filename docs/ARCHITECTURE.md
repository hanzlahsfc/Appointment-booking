# Architecture — how the system works

This document explains the crawler end to end: control flow, data model, WAF
handling, and the slot-detection pipeline.

## 1. System overview

```
                    ┌──────────────────────────────────────────────────┐
   asyncio loop     │  CrawlerWorker (one per profile)                │
   ──────────────▶  │  • Playwright context (isolated cookies)        │
                    │  • login / session restore (storage_state)      │
                    │  • fetch slots (DOM or XHR, recon-driven)       │
                    │  • normalize → diff vs SQLite                   │
                    │  • NEW slot → enqueue alert task                │
                    └───────────┬──────────────────┬──────────────────┘
                                │                  │ dashboard push
                                ▼                  ▼
                    ┌─────────────────────────┐  ┌──────────────────────┐
                    │    Notifier (Resend)   │  │ DashboardState →     │
                    │  HTML email to inbox   │  │ FastAPI → web UI     │
                    └─────────────────────────┘  └──────────────────────┘
```

Everything lives in **one process**, one `asyncio` event loop:

- `main.py` starts the scheduler + a **uvicorn server task** for the dashboard.
- Each profile runs a `CrawlerWorker` task in a `Semaphore`-gated async job.
- Workers push status/slots/alerts into `DashboardState` (in-memory, thread-safe);
  the FastAPI endpoints (`/`, `/api/status`) read snapshots from it.
- On SIGINT/SIGTERM the loop cancels workers, closes browser contexts, and stops
  uvicorn.

## 2. Module responsibilities

### `app/browser.py` — browser factory + WAF
- `create_browser(headless)` launches Chromium with
  `--disable-blink-features=AutomationControlled`, `--no-sandbox`.
- `new_context(...)` makes a fresh context per profile with a fixed realistic
  UA/viewport, `timezone_id="Asia/Karachi"`, and optionally a persisted
  `storage_state` (cookies) so the WAF doesn't re-challenge on every poll.
- `IncapsulaHandler.wait_for_clear(page)` polls until the URL no longer shows the
  Incapsula shell (`_Incapsula_Resource`, `_/Incapsula_` markers) **and** expected
  content appears; raises `WAFError` after `max_wait`.

### `app/session.py` — login & session lifecycle
- Credentials per profile live in `data/creds/<slug>.json` (`{username,password}`).
- First run: `ensure_logged_in()` drives a **headful** login once, then persists
  `context.storage_state()` to `state/<slug>.json`. All subsequent runs restore
  that file and skip login.
- `check_session_still_valid()` detects a bounced back to `/login/PK/en` (expired
  session) and re-logs-in automatically on the next poll.
- The post-login redirect marker is `LOGGED_IN_EXPECT` (currently
  `/en/account`) — **adjust after recon (§2.4/§11)**.

### `app/crawler.py` — the worker loop
Real mode, per poll:

```
sleep (poll_interval ± jitter)
→ open booking_url
→ if bounced to login: re-login
→ wait for Incapsula to clear
→ extract slots          (recon-dependent — see §6)
→ normalize → Store.upsert_slots → returns NEW
→ deactivate missing slots
→ for each NEW: spawn _dispatch_alert task
→ push status/slots/alerts to DashboardState
→ log: OK | profile=… | slots=… | new=… | dur=…s
```

Demo mode (`--demo`): skips the browser entirely; `_demo_seed()` fabricates
work-permit/premiere-launch slots within the Thu–Fri release window, ticks changes
every 3rd poll (add/remove/shift), and pushes alerts so the dashboard looks alive.

### `app/store.py` — SQLite state
Two tables:

```sql
slot_state(profile, location, category, date, time, raw,
           first_seen, last_seen, alerted, active,
           PRIMARY KEY(profile, location, category, date, time));
alert_log(  id, profile, location, category, date, time,
            sent_at, subject, status);
```

- `upsert_slots()` both refreshes rows and **returns alertable ones**:
  - never seen before → NEW
  - previously seen, then disappeared (`active=0`) and **never alerted** → NEW
  - already alerted → never alerted again, even if re-added
- `deactivate_missing()` / `active` column tracks **presence** so that a slot
  that leaves the site doesn't keep suppressing a future re-add alert.
- `mark_alerted()` sets `alerted=1` **only after a successful send** (a failed
  send can still alert later via the re-add path).
- Writes are serialized with a lock (multiple worker tasks share the connection).

### `app/notifier.py` — Resend email
- `POST https://api.resend.com/emails` with `Authorization: Bearer <key>`.
- Subject: `🟢 NEW SLOT: {category} @ {location} — {date}`
- HTML body includes profile, category, VAC, date/time, a CTA button to the
  booking URL, and a "manual booking / slot may vanish" disclaimer.
- 3 attempts with backoff on errors; non-2xx or network failure → returns
  `False`, and the alert is logged with `status=failed`.
- In dry-run, `send()` only logs `[dry-run] would send …`.

### `app/dashboard.py` + `app/web.py` + `static/index.html` — the dashboard
- `DashboardState` keeps per-profile live status, the current slot list, and a
  bounded alert feed (in-process, lock-protected).
- FastAPI serves the single-page UI and `GET /api/status` (JSON snapshot).
- The page refresh-polls every 2 s; shows profile cards, active slots table, and
  the latest 50 alerts. Dark theme, Plus Jakarta Sans, green "NEW/alive" accents.
- Dashboard is cosmetic: if it's down, the crawler keeps polling and emailing.

## 3. Diffing rules (truth table)

| Poll A | Poll B | Result |
|---|---|---|
| X present | X present | no alert |
| X absent | X added | **alert** (X, since first_seen null) |
| X present | X gone | no alert (removed ≠ new) |
| X gone (not alerted) | X returns | **alert** (active 0 → 1, never alerted) |
| X gone (previously alerted) | X returns | no alert (alerted=1 persists) |
| restart, recall known X | X present | no alert (state is in SQLite) |

## 4. Time, jitter, pace

- All state timestamps use `Asia/Karachi` (PKT).
- Poll base interval `poll_interval_seconds` (default 300 s) with uniform
  random `± jitter_seconds`. Purpose: don't look like a bot. The operator who
  misuses this can get slots cancelled — keep rate discipline.

## 5. Email pipeline

```
NEW slot → _dispatch_alert (async task, off the browser loop)
        → notifier.send()
        → success?  mark_alerted + alert_log(status=sent) + dashboard push
        → failure?  alert_log(status=failed) + dashboard push(failed)
```

Dry-run short-circuits before any DB write or network send.

## 6. Slot extraction — recon-gated (read this)

This is the only piece still **not implemented against the real site**, because
the post-login DOM/XHR cannot be inspected from the flagged development network
(see README → "The WAF/IP block").

Current placeholders in `app/crawler.py`:

- `SLOT_SELECTOR_HINT = None` — row selector for the fallback DOM scraper
- `_scrape_dom()` — generic `.slot-row` heuristic
- `_parse_slot_label()` — expects `"YYYY-MM-DD HH:MM"` text per row
- `session.LOGGED_IN_EXPECT = "/en/account"` — post-login redirect marker

**Required recon pass (§2.4):** from a WAF-clean network with real credentials,
log in headfully via Playwright, capture the appointment-page DOM + any XHR/JSON
the slot-picker fires. **If an internal JSON/XHR endpoint exists, prefer it** —
parsing it is faster and less fragile than DOM scraping. Record findings in the
sqlite logic / fill the constants, then update `tests/` with realistic fixtures.

## 7. Failure handling

| Failure | Behavior |
|---|---|
| WAF never clears | `WAFError` → poll logged as failed, dashboard shows error, retries next poll |
| Session expired | detected next poll → throttled re-login → continue |
| One profile crashes | worker logs the error; because the loop treats this as fatal the app exits (Docker/systemd restart it automatically) |
| Email API down | alert logged `failed`; retried next time it appears (re-add path) |
| No `RESEND_API_KEY` in real mode | refusal to start (exit code 1) |

## 8. Security model

- Credentials: `data/creds/*.json`, `chmod 600`, git-ignored.
- Secrets: `.env` only, never in `config.yaml`.
- VPS: no public ports except the closed-loop dashboard (bind `127.0.0.1`);
  key-only SSH; container drops all capabilities and uses `no-new-privileges`.
- No auto-booking: the crawler only reads; humans book.
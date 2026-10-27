# Bridge recon — live portal checks through your signed-in Chrome

`bridge-recon` verifies the real portal **read-only** through the CDP
Bridge extension in your own Chrome — the only path that works while this
machine's egress IP is WAF-flagged. It never books, never requests OTP,
never copies cookies or credentials, and never touches another tab.

## Prerequisites

- The **CDP Bridge** extension loaded in Chrome (devtools-extension repo,
  `extension/` dir) with the native host registered — its HTTP API lives
  at `http://localhost:1232`.
- If the API stops answering (`curl http://localhost:1232/health` fails):
  `chrome://extensions` → CDP Bridge card → **reload (↺)**. The native host
  only starts when the extension's service worker connects.

## Startup

```bat
:: 1. find your signed-in portal tab
python -m app.hosted bridge-recon --list-tabs --tab 0 --origin x

:: prints:   413653551  https://pk-gr-services.gvcworld.eu/appointments/add

:: 2. run the check against THAT tab
python -m app.hosted bridge-recon ^
    --tab 413653551 ^
    --origin https://pk-gr-services.gvcworld.eu ^
    --category 26 ^
    --target-date 30/11/2026
```

`--tab` is mandatory and explicit — the run stops if the tab id does not
exist; it never falls back to a different tab. `--origin` must be the
tab's exact origin (scheme+host+port); if the tab navigates anywhere else
mid-run, the run aborts with `BridgeNavRejected`.

## What it does (and only this)

1. `health` + `tabs` — bridge alive, tab present on the expected origin
2. **stop** if the page shows a login form or a bare `401` page
   (`BridgeSessionLost`) or a challenge frame (`BridgeChallenge`)
3. assert every selector `GvcwSiteDriver` depends on — a missing element
   means portal drift and is reported by name
4. verify `--category` exists in `#type`
5. ONE normal availability Search (the same `PUT /api/v1/periodslot/slots`
   the page issues on Search click) and report the grid: slot ids, times,
   enabled vs disabled, and whether the empty-state marker showed
6. save a redacted evidence JSON under `data/recon/bridge-recon-*.json`
   (no cookie headers, no credentials)

Trusted CDP input (`Input.dispatchMouseEvent`) is used only for the Search
click — the same click a human makes. No OTP request, no submit click,
no applicant entry.

## Exit codes / errors

- `0` — checks passed (see `ok`, `checks`, `slots`, `bookable`, `empty`)
- `1` — check failed (`stage` tells which: `bridge`/`tab`/`selectors`/`category`/`search`)
- `2` — stopped by a safety gate: `BridgeDisconnected` (tab closed/detached,
  bridge host down), `BridgeNavRejected` (tab left the allowlist),
  `BridgeSessionLost` (sign back in), `BridgeChallenge` (solve it by hand)

## Limitations

- **One tab, one operator.** This is a local testing channel into YOUR
  session — it is deliberately NOT wired into the hosted multi-client
  dashboard, and must not be.
- ~3 HTTP round-trips per operation (tab check + session check + call) —
  fine for recon cadence, too chatty for tight polling loops.
- The page object is a duck-typed subset — `evaluate`, `click`, `goto`,
  `wait_for_*`, `locator().count/check/is_checked/input_value/is_visible/click`,
  `screenshot`, `content`. Everything `GvcwSiteDriver` uses, nothing more.
- Ambiguous submit outcomes are never retried — same contract as the
  hosted path.
- Booking actions exist only behind a future explicit operator flag; the
  recon command is observation-only by construction.

## Recovery

| symptom | action |
|---|---|
| `bridge unreachable` | reload the extension card; verify `curl :1232/health` |
| `tab N not found` | `--list-tabs` and pick the current id |
| `BridgeSessionLost` | log in on the tab, re-run |
| `BridgeChallenge` | solve the challenge in that tab, re-run |
| `missing selectors` | portal changed — re-decode and update `app/gvcw.py` |
| bridge host wedged | `chrome://extensions` → reload; worst case `node install.js` re-run in the repo |

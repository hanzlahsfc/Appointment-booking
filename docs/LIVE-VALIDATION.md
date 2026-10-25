# Supervised live validation — Windows runbook

Everything below is read-only or local. No step in §1–§6 reserves or submits
anything, and no emails are sent while `dry_run` is on. Real reservation is a
separate, explicitly authorized step (§7).

## 1. Install (Windows)

```powershell
cd C:\Users\Hanzlah\Desktop\appointment-booker
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # includes websockets — required for /bridge
.venv\Scripts\playwright install chromium       # only needed for driver: "playwright" or recon.py
```

## 2. Extension + crawler config

1. `chrome://extensions` → Developer mode → **Load unpacked** → select `extension\`.
2. `config.yaml`:
   ```yaml
   app:
     driver: "bridge"        # under app:, default — drives your signed-in tab
     dry_run: true           # monitoring dry-run: no emails
     booking:
       mode: "assist"        # or "off"; assist claims then hands off
       dry_run: true         # booking dry-run: never clicks reserve
   ```
3. Start: `.venv\Scripts\python main.py --skip-preflight`
   (skip preflight: its plain-HTTP GET will show the WAF block even when the
   operator's browser works fine — it tests the *crawler's* egress, not yours).
4. Extension popup → pick the signed-in portal tab → **Connect**.
   Optional: set `AB_BRIDGE_TOKEN` in the crawler's environment and paste the
   same value into the popup's token field.

Verify: `http://127.0.0.1:8080/api/bridge` shows `"connected": true` and your
`selectedTab`. `/api/status` shows per-profile OK/ERR.

## 3. Dry-run guarantees (verified)

With `app.dry_run: true` **or** `app.booking.dry_run: true`:
- `BookingAgent.book()` returns `dry_run` **before** `driver.reserve()` —
  nothing is clicked.
- Emails are logged as `[dry-run] would send …`, never sent.
- Monitoring still navigates and reads the portal page in your tab — that is a
  normal read, not a reservation.

## 4. Selector recon — bridge path (no CDP needed)

The extension attaches Chrome's debugger to the driven tab on the first
command and captures request/response traffic per tab.

```powershell
# while the crawler runs and the extension is connected:
# 1. click through the picker yourself: VAC → visa type → dates (NO booking)
# 2. dump what the tab sent/received:
Invoke-RestMethod "http://127.0.0.1:8080/api/bridge/network?tab=<tabId>&limit=200" |
  ConvertTo-Json -Depth 8 | Out-File recon\bridge-network.json
```

Then fill `recon\booking-selectors.json` from `docs\booking-selectors.example.json`
using the observed picker DOM/XHR. Reload is automatic — selectors are re-read
each poll.

### Fallback: recon.py via CDP (only if the bridge capture is insufficient)

Chrome ≥136 ignores `--remote-debugging-port` on your *default* profile, so a
dedicated profile is mandatory:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\chrome-recon"
# in that window: sign in to the portal manually (reCAPTCHA included)
.venv\Scripts\python recon.py --cdp http://127.0.0.1:9222
# click through the picker; Ctrl+C to stop → recon\booking-flow.json
```

## 5. Slot formats actually supported

`_parse_slot_label` accepts, anywhere in a row's text:

| Format | Example |
|---|---|
| ISO | `2026-12-01 09:00` |
| DD.MM.YYYY | `01.12.2026 10:30` |
| DD/MM/YYYY or DD-MM-YYYY | `1/12/26 9:00` |

Time `HH:MM` is optional; missing time stores `""` (no-time). Garbage rows and
impossible dates (`32.13.2026`) are skipped.

**Visible failure:** if rows matched `slot_row` but *none* parsed, the worker
logs `WARNING … N slot rows matched but NONE parsed` — check `slot_row` first.
Partially-unparsed rows log `INFO … k/N rows unparsed`.

## 6. What a healthy dry-run looks like

```
OK | profile=Ahmed - Work Permit | slots=0 | new=0 | dur=1.2s
[dry-run] would send to …: 🟢 NEW SLOT: …   (only when a slot appears)
```

Dashboard profile card stays OK; errors show the mapped cause (extension down /
tab busy / session expired / WAF block / selectors missing / picker mismatch).

## 7. Assisted booking — separate, authorized step

Only after §1–§6 pass and a real slot is actually offered:

1. Set `app.booking.mode: "assist"`, `booking.dry_run: false`, profile
   `auto_book: true`, `app.dry_run: false` (emails armed — or keep `dry_run`
   true to suppress email while still exercising the claim path).
2. On the next matching slot the agent clicks **reserve**, alarms, and parks:
   status `needs_human`, tab untouched, polling blocked.
3. You complete the applicant form by hand in that tab.
4. Afterwards: `python main.py --list-bookings` then
   `python main.py --resolve-booking <id>` to release the profile's block.

Capturing a *real* reservation's network trace is its own authorized action —
do it once, deliberately, in the §4 bridge capture or recon.py window.

## Evidence & limits (honest)

- Mock-portal tests prove: reserve click → slot `held` until submit; second
  claim refused. **Real portal hold duration is unverified** — observe it live
  during the first supervised assist.
- The `detect→reserve` latency test measures in-process driver time only
  (~ms). Real performance = that + bridge round-trips + portal page loads +
  WAF, i.e. seconds-scale; do not quote the mock number as portal performance.

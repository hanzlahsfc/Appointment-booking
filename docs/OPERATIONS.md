# Operations

How to run, monitor, troubleshoot, and complete the mandatory recon pass.

## 1. Day-to-day

```bash
# start (real mode)
.venv/bin/python main.py

# start (demo/showcase)
.venv/bin/python main.py --demo --port 8080

# view dashboard locally
open http://127.0.0.1:8080
# view dashboard on the VPS through an SSH tunnel
ssh -L 8080:127.0.0.1:8080 root@<server-ip>
```

Expected log line every poll:

```
OK | profile=Ahmed - Work Permit | slots=4 | new=1 | dur=3.8s
```

## 2. Adding / changing profiles

1. Edit `config.yaml` → add/change a `profiles:` entry (unique `name`, target
   `category` and `location`, recipient `email`).
2. Create the credential file `data/creds/<Slugged_Name>.json`:
   ```bash
   echo '{"username":"…","password":"…"}' > data/creds/Ahmed_-_Work_Permit.json
   chmod 600 data/creds/Ahmed_-_Work_Permit.json
   ```
3. Delete `state/<Slugged_Name>.json` if it exists (forces a fresh login).
4. Restart `main.py`. The first poll triggers the one-time login.

Slug = profile name with every non-alphanumeric run replaced and dashes kept:
`"Ahmed - Work Permit"` → `Ahmed_-_Work_Permit`.

## 3. Categories & VACs (from the public schedule)

- Monday–Friday: National D visa — Seasonal & Dependent Employment
- Thursday–Friday: National D (Employment & Rest) + Schengen Type C
- VACs: **Islamabad**, **Lahore**; submissions 09:00–15:00

This build tracks **Work Permit (`national_d_employment`)** and **Premiere
Launch (`national_d_seasonal`)** slots.

## 4. Email (Resend)

- Key: `.env` → `RESEND_API_KEY` (from https://resend.com, free tier).
- Sender: `RESEND_FROM` or `notifications.from` must be a verified domain in
  Resend, e.g. `GVCW Slot Alert <oncall@yourdomain.com>`.
- First real email: send a manual **test alert** and confirm it lands in the
  client's inbox (not spam). Check SPF/DKIM per Resend's instructions.
- In `dry_run: true`, no email is sent — only `[dry-run] would send …` logs.

## 5. The recon pass (§2.4 / §11 — REQUIRED before production)

The crawler's slot extraction is stubbed until someone inspects the real
post-login page. Steps, from a **WAF-clean network** with a real profile:

```bash
# temporarily open the browser headful
#  config.yaml → app.headless: false
.venv/bin/python main.py
```

1. Let the one-time login complete; confirm it lands past
   `…/login/PK/en` (note the real redirect URL — compare with
   `session.LOGGED_IN_EXPECT`).
2. Navigate to that profile's `booking_url`.
3. In the dashboard/devtools (or a Playwright script with
   `page.on("response")`), capture:
   - the slot-picker DOM structure and selectors,
   - **any XHR/JSON the picker calls** — preferred over DOM scraping,
   - how dates/times are rendered (label formats).
4. Fill in:
   - `session.LOGGED_IN_EXPECT` — real redirect marker,
   - `crawler.SLOT_SELECTOR_HINT` and/or an XHR parser,
   - `_parse_slot_label()` for the real label formats.
5. Re-run with `dry_run: true` and confirm the logged slots match what a human
   sees in the browser (acceptance §10.2). Then disable dry-run for a live alert.

Record the findings in README §"Recon notes".

## 6. WAF reachability check (from any host)

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://pk-gr-services.gvcworld.eu/login/PK/en" \
  -H "User-Agent: Mozilla/5.0"
```

| Result | Meaning |
|---|---|
| 302/200 | likely fine — proceed to browser test |
| 403 | IP **may** be flagged; browser test decides |
| challenge frame "Error 17 / Access denied" | **hard IP block** — change network/proxy |

`Error 17` is IP-reputation based; real Chrome is blocked too, so no in-browser
trick fixes it. Options: switch to another egress (home ISP, mobile, clean VPS
IP), or add a residential proxy.

## 7. Troubleshooting

| Symptom | Cause → Fix |
|---|---|
| `unrecognized arguments: # open http…` | comment pasted into the command line → drop the `# …` text |
| `no matches found: data/creds/*.json` | zsh expands the glob before Python sees it → create the file first, or quote it |
| `zsh: command not found` | venv not activated → use `.venv/bin/python` |
| `incap_ses_*` then empty page on the portal | WAF/IP block → see §6 |
| Worker logs a `WAFError` every poll | challenge not clearing → the egress IP is being challenged/blocked |
| `missing env RESEND_API_KEY` | `.env` not created or key env name mismatch in `config.yaml` |
| Dashboard shows ERR / no data | real mode can't browse (WAF) or profiles not configured → check logs, then §6 |
| Login "did not redirect (expected /en/account…)" | `LOGGED_IN_EXPECT` wrong → fix after recon (§5) |
| Emails marked spam | Resend domain not verified / missing SPF-DKIM → fix in Resend |
| Duplicate alerts after restart | should not happen (SQLite). If DB was deleted, re-alerts are expected by design |

## 8. Testing

```bash
.venv/bin/python -m pytest tests
```

11 tests cover the acceptance clauses in §10.1:

- same slots → no alert
- new slot → alert
- slot removed → no alert
- re-added removed slot alerted **only if never alerted before**
- restart → no duplicate alerts for known slots

## 9. Security checklist

- [ ] `.env` and `data/creds/*.json` never committed (see `.gitignore`)
- [ ] credential files `chmod 600`
- [ ] VPS: no public ports; SSH key-only, no password
- [ ] Rate discipline respected (default 5-min ± jitter — do not lower aggressively)
- [ ] No auto-booking code enabled
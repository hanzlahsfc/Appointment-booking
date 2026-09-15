# Deployment & infrastructure

Where to run the crawler, what it needs, and how to provision it.

## 1. Resource requirements

| Resource | Need | Why |
|---|---|---|
| CPU | 2 vCPU | async + one Chromium context per profile |
| RAM | ≥ 2 GB (4 GB recommended) | headless Chromium ~150–300 MB per context |
| Disk | ≥ 10 GB | Python + Playwright + Chromium + SQLite/history |
| Network | outbound HTTPS only | polls the portal, sends emails |
| Public ports | none (dashboard binds loopback) | keep attack surface closed |

## 2. Hosting options (≤ $5/mo budget)

| Option | Cost | Verdict |
|---|---|---|
| **Hetzner Cloud CX22** | ~€3.79–4.29/mo (~$4.50) | **Recommended** — 2 vCPU / 4 GB / 40 GB NVMe, APAC-adjacent (Singapore) or EU (Falkenstein/Helsinki) locations |
| Oracle Cloud Always Free (ARM Ampere) | $0 | Most powerful free option (4 OCPU / 24 GB); signup friction + keep-alive policy; ARM64 works with Playwright |
| Contabo | ~$4–5/mo | More cores/RAM for the money but slower network; fine as backup |
| DigitalOcean / Vultr $4–6 tiers | $4–6 | Too RAM-limited for Chromium; avoid |

**Oracle exact specs and Hetzner pricing change; verify before ordering.**

## 3. WAF reachability check (BEFORE you pay)

The portal blocks the development egress IP at the network level (README → "The
WAF/IP block"). On a fresh host, verify first that the IP is not flagged **or**
you will pay for a box that can't reach the login page:

```bash
# from the new VPS:
curl -o /dev/null -s -w "%{http_code}\n" \
  "https://pk-gr-services.gvcworld.eu/login/PK/en" \
  -H "User-Agent: Mozilla/5.0"
# 403 → IP may be flagged (continue to browser test)
# 302/200 → promising
```

Then, more definitively, run the headful login from the host (see
`docs/OPERATIONS.md` → "Recon pass"). If the challenge frame shows
`Error 17` / `Access denied`, the IP is hard-blocked → use a residential proxy
or host on the client's home broadband instead.

## 4. Docker deployment (Hetzner)

Provision (Ubuntu 22.04/24.04):

```bash
ssh root@<server-ip>
apt update && apt install -y docker.io docker-compose-plugin ufw
ufw allow OpenSSH && ufw enable
```

Deploy:

```bash
# local machine
rsync -av --exclude .venv --exclude data --exclude state --exclude .git \
  crawler/ root@<server-ip>:/opt/gvcw-crawler/

# server
cd /opt/gvcw-crawler
cp .env.example .env        # then fill RESEND_API_KEY
mkdir -p data/creds state
# put <profile>.json credential files into data/creds/

cd deploy
docker compose up -d --build
docker compose logs -f --tail 50
```

Notes:

- `docker-compose.yml` mounts `./data` and `./state` (relative to `deploy/`) into
  `/app/data` and `/app/state` — the DB, credentials and persisted sessions
  survive container rebuilds.
- `restart: unless-stopped` + `cap_drop: ALL` + `no-new-privileges`.
- The Dashboard is bound to loopback in the container (default) — SSH
  `-L 8080:127.0.0.1:8080` to view it from your laptop.

## 5. systemd deployment (alternative)

```bash
# server: python3.11 + venv
apt install -y python3.11 python3.11-venv
adduser --system gvcw
mkdir -p /opt/gvcw-crawler && chown gvcw:gvcw /opt/gvcw-crawler

sudo -u gvcw bash -c "
  python3.11 -m venv /opt/gvcw-crawler/.venv
  /opt/gvcw-crawler/.venv/bin/pip install -r /opt/gvcw-crawler/requirements.txt
  /opt/gvcw-crawler/.venv/bin/playwright install chromium --with-deps
"

cp deploy/gvcw-crawler.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gvcw-crawler
journalctl -u gvcw-crawler -f
```

The unit: `Restart=always` (`RestartSec=10`), logs to
`/var/log/gvcw-crawler.log`, `EnvironmentFile=/opt/gvcw-crawler/.env`.

## 6. Monitoring & health

- **Dashboard**: `http://127.0.0.1:8080` (tunnel via SSH). Green `LIVE` dot +
  per-profile OK/ERR.
- **Process health**: Docker `restart: unless-stopped` / systemd `Restart=always`
  restart the app; the app logs `OK | profile=… | slots=… | new=… | dur=…s` per
  poll.
- **Alert delivery**: `alert_log` table + dashboard feed; a failed send is
  surfaced with a `FAILED` badge.
- Optional external uptime checks: `curl` the tunneled `/api/status`, or a cron
  that greps the log for recent `OK` lines.

## 7. Backups

The only state worth backing up is `data/slot_state.db` (alert history) —
credentials and storage_state can be regenerated. A weekly `tar`/`rclone` of
`data/` to an object store is sufficient. Deleting the DB deliberately (fresh
start) will also re-alert every currently visible slot.
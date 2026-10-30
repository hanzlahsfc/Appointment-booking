"""Telegram relay — one bot + one group chat is the bus between every
machine running the app and the operator's phone.

No tunnels, no public IP, no port forwarding: machines only make outbound
HTTPS calls to api.telegram.org, which works behind any NAT/firewall.

All machines share the same bot token and poll ``getUpdates`` — and this is
the subtle part: passing an ``offset`` to getUpdates CONFIRMS every update
below it, which would let one machine eat another machine's commands. So we
always poll ``offset=0`` (never confirm anything) and dedupe update ids
locally. Telegram keeps unconfirmed updates for ~24h, far longer than any
command needs to live.

Commands (posted in the group chat):
  /status              every machine replies with its status
  /status ID           only machine ID replies
  /queue [ID]          queue listing
  /confirm ID          press Confirm-and-book on ID's machine
  /pause ID · /resume ID · /start ID · /stop ID   monitor control

Machine ID: persisted in data/machine-id — <hostname>-<4hex>.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import socket
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_CMD = re.compile(r"^/([a-zA-Z_-]+)(?:\s+(\S+))?(?:\s+(.*))?$")
_NOTABLE = {  # feed statuses that generate a group message
    "selected_awaiting_human", "otp_sent", "otp_entered", "otp_rejected",
    "otp_request_failed", "auto_confirm_failed", "booked",
    "outcome_unknown", "failed", "queue_finished", "bridge_error",
    "error", "reset_error", "portal_recovered",
}
_ALERT_COOLDOWN = 600  # seconds between repeated alerts of the same kind


def machine_id(data_dir: Path) -> str:
    """Stable per-machine id, e.g. 'NADIR-A4F2'."""
    p = data_dir / "machine-id"
    if p.exists():
        return p.read_text().strip()
    host = re.sub(r"[^A-Za-z0-9]", "", socket.gethostname()).upper()[:8] or "PC"
    mid = f"{host}-{secrets.token_hex(2).upper()}"
    p.write_text(mid)
    return mid


class TelegramAgent:
    def __init__(self, monitor, queue, token: str, chat_id: str, mid: str,
                 port: int = 8080):
        self.monitor = monitor
        self.queue = queue
        self.token = token
        self.chat_id = str(chat_id)
        self.mid = mid
        self.port = port
        self._seen: set[int] = set()
        self._last_status: str | None = None
        self._ev_ts = 0.0               # last processed feed-event timestamp
        self._alert_at: dict = {}       # kind -> last alert time (cooldown)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._http = None  # httpx.AsyncClient, created inside the loop

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._http:
            await self._http.aclose()

    # ── telegram api ─────────────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return _API.format(token=self.token, method=method)

    async def send(self, text: str) -> None:
        try:
            await self._http.post(self._url("sendMessage"), json={
                "chat_id": self.chat_id, "text": text,
                "disable_web_page_preview": True,
            })
        except Exception as exc:
            logger.warning("telegram send failed: %s", exc)

    async def _updates(self) -> list[dict]:
        # offset=0 — never confirm updates; shared-token machines would
        # otherwise consume each other's commands. Deduped by self._seen.
        try:
            r = await self._http.get(self._url("getUpdates"),
                                     params={"offset": 0, "limit": 100})
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except Exception as exc:
            logger.warning("telegram poll failed: %s", exc)
            return []

    # ── main loop ────────────────────────────────────────────────────────

    async def _run(self):
        import httpx
        self._http = httpx.AsyncClient(timeout=15)
        await self.send(f"[{self.mid}] STARTED — app running on "
                        f"127.0.0.1:{self.port} · pair the portal tab, then "
                        f"/status /captcha /confirm /pause /resume")
        while not self._stop.is_set():
            try:
                await self._poll_commands()
                await self._watch_feed()
            except Exception as exc:
                logger.warning("telegram loop error: %s", exc)
            await asyncio.sleep(4)

    async def _poll_commands(self):
        for upd in await self._updates():
            uid = upd.get("update_id")
            if uid in self._seen:
                continue
            self._seen.add(uid)
            if len(self._seen) > 5000:
                self._seen = set(list(self._seen)[-2500:])
            msg = upd.get("message") or upd.get("edited_message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                await self._handle(text)

    async def _watch_feed(self):
        # scan every new feed event — errors like "session expired" or a
        # collapsed portal must reach the phone even though they don't
        # change the headline status
        for ev in (self.monitor.feed.get("events") or []):
            ts = ev.get("ts", 0)
            if ts <= self._ev_ts:
                continue
            self._ev_ts = ts
            await self._alert_on(ev)

        status = self.monitor.feed.get("status")
        if status == self._last_status or status not in _NOTABLE:
            return
        self._last_status = status
        ev = (self.monitor.feed.get("events") or [{}])[-1]
        cur = self.queue.current() or {}
        name = (cur.get("data") or {}).get("display") or cur.get("name") or "?"
        extra = ""
        if ev.get("slot"):
            extra += f" · slot {ev['slot']}"
        if ev.get("ref"):
            extra += f" · ref {ev['ref']}"
        if ev.get("error"):
            extra += f" · {str(ev['error'])[:140]}"
        if ev.get("after_errors"):
            extra += f" · after {ev['after_errors']} failed checks"
        if status == "selected_awaiting_human":
            extra += ("\n→ the APK on the client's phone auto-enters the "
                      "code — this machine books it. Fallback: captcha on "
                      f"the machine, then /confirm {self.mid}")
        elif status == "otp_sent":
            extra += "\n→ SMS is on the client's phone — APK auto-enters"
        await self.send(f"[{self.mid}] {status} · {name}{extra}")

    async def _alert_on(self, ev: dict):
        """Cooldown-guarded alerts for event-level problems."""
        if ev.get("status") != "search_error":
            return
        err = str(ev.get("error") or "")
        if "session expired" in err:
            kind, text = "session", (
                "SESSION EXPIRED — the portal logged out. Sign back in on "
                "the portal tab of this machine, monitoring keeps running.")
        else:
            kind, text = "portal_down", (
                "portal not responding — the site is overloaded or "
                "blocking. Backing off; it usually recovers in ~1-5 min.")
        now = time.time()
        if now - self._alert_at.get(kind, 0) < _ALERT_COOLDOWN:
            return
        self._alert_at[kind] = now
        await self.send(f"[{self.mid}] {text}")

    # ── commands ─────────────────────────────────────────────────────────

    async def _handle(self, text: str):
        m = _CMD.match(text)
        if not m:
            return
        cmd, target, rest = m.group(1).lower(), m.group(2), m.group(3)

        # broadcast commands answer for everyone when no target is given;
        # anything targeted at a different machine id is dropped here —
        # every machine sees every group message.
        broadcast = cmd in {"status", "queue", "list", "help"}
        if target and target.upper() != self.mid:
            return  # addressed to another machine (or bad broadcast)

        if cmd in {"help", "start"}:
            await self.send(
                f"[{self.mid}] commands:\n"
                "/status [ID] — machine status\n"
                "/queue [ID] — applicant queue\n"
                "/captcha ID — auto-solve the captcha\n"
                "/confirm ID — press Confirm and book\n"
                "/pause ID · /resume ID · /stop ID")
            return

        if cmd == "status":
            s = self.monitor.snapshot()
            q = s["queue"]
            cur = s["current"]
            cname = ((cur or {}).get("data") or {}).get("display") or "-"
            counts = {}
            for row in q:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            summary = ", ".join(f"{k}:{v}" for k, v in counts.items()) or "empty"
            await self.send(
                f"[{self.mid}] {s['status']}"
                f"{' PAUSED' if s['paused'] else ''}"
                f" · tab {'paired' if s['tab_id'] else 'unpaired'}"
                f" · now: {cname} · queue: {summary}")
            return

        if cmd in {"queue", "list"}:
            rows = self.queue.list()
            if not rows:
                await self.send(f"[{self.mid}] queue empty")
                return
            lines = []
            for i, r in enumerate(rows[:15], 1):
                name = (r.get("data") or {}).get("display") or "?"
                lines.append(f"{i}. {name} — {r['status']}")
            await self.send(f"[{self.mid}] queue:\n" + "\n".join(lines))
            return

        if cmd == "captcha":
            await self.send(f"[{self.mid}] solving captcha (30-90s)…")
            r = await self.monitor.solve_captcha()
            await self.send(f"[{self.mid}] " +
                            ("captcha token injected — /confirm "
                             f"{self.mid} to finish" if r.get("ok")
                             else f"captcha solve failed: {r.get('error')} "
                                  "— solve it on the machine"))
            return

        if cmd == "confirm":
            r = await self.monitor.confirm_book()
            if r.get("ok"):
                await self.send(f"[{self.mid}] BOOKED ✓ "
                                f"ref {r.get('confirmation_ref')}")
            else:
                await self.send(f"[{self.mid}] confirm failed — parked for "
                                f"review: {r.get('error')}")
            return

        if cmd in {"pause", "resume", "stop", "start"}:
            if cmd == "pause":
                self.monitor.paused = True
            elif cmd == "resume":
                self.monitor.paused = False
            elif cmd == "stop":
                await self.monitor.stop()
                self.monitor._task = None
            elif cmd == "start":
                if self.monitor._task is None or self.monitor._task.done():
                    self.monitor._stop.clear()
                    self.monitor.start()
            await self.send(f"[{self.mid}] {cmd} ok")
            return


def make_agent(monitor, queue, data_dir: Path,
               token: str | None, chat_id: str | None,
               port: int = 8080) -> TelegramAgent | None:
    """Build an agent if the operator configured a token; silent otherwise."""
    import os
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return TelegramAgent(monitor, queue, token, chat_id,
                         machine_id(data_dir), port=port)

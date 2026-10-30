"""OTP ingest via Firebase Realtime Database — the client-side APK writes
the SMS code to a shared path; this machine polls, consumes, deletes it.

Protocol (pure REST — no Firebase SDK, nothing added to the wheels):
    APK writes:  PUT   <db>/otps/<secret>/<machine_id>.json   "123456"
    We poll:     GET   same path → JSON string → submit → DELETE node

The secret is a long random path component shared by the APK and machines —
anyone who can't guess it can't read or poison the channel.

On a successful OTP entry the full chain runs automatically: captcha
auto-solve (if a key is configured) → confirm_and_book. The APK exists so
there is no human in the loop — a confirmed slot is booked, not parked
waiting for a phone.
"""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

_CODE = re.compile(r"^\s*\"?(\d{4,8})\"?\s*$")


class OtpChannel:
    def __init__(self, monitor, db_url: str, secret: str, mid: str,
                 poll_s: float = 2.0, auto_confirm: bool = True):
        self.monitor = monitor
        self.db_url = db_url.rstrip("/")
        self.secret = secret
        self.mid = mid
        self.poll_s = max(1.0, poll_s)
        self.auto_confirm = auto_confirm
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._http = None

    def _node(self) -> str:
        return f"{self.db_url}/otps/{self.secret}/{self.mid}.json"

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._http:
            await self._http.aclose()

    async def _run(self):
        import httpx
        self._http = httpx.AsyncClient(timeout=15)
        logger.info("otp channel: polling %s every %.1fs",
                    self._node(), self.poll_s)
        while not self._stop.is_set():
            try:
                await self._poll()
            except Exception as exc:
                logger.warning("otp channel error: %s", exc)
            await asyncio.sleep(self.poll_s)

    async def _poll(self):
        r = await self._http.get(self._node())
        if r.status_code != 200 or r.text in ("null", ""):
            return
        m = _CODE.match(r.text)
        if not m:
            return  # garbage in the node — leave it for the operator
        code = m.group(1)
        # consume first so a second poll can't re-enter a stale code
        await self._http.delete(self._node())
        logger.info("otp channel: received code for %s", self.mid)
        r2 = await self.monitor.submit_otp(code)
        if not r2.get("ok"):
            logger.warning("otp channel: submit failed: %s", r2.get("error"))
            self.monitor._say("otp_rejected", source="apk",
                              error=r2.get("error"))
            return
        self.monitor._say("otp_entered", source="apk")
        if self.auto_confirm:
            if self.monitor.captcha_api_key:
                await self.monitor.solve_captcha()
            r3 = await self.monitor.confirm_book()
            if not r3.get("ok"):
                self.monitor._say("auto_confirm_failed",
                                  error=r3.get("error"))

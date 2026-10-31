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
_DIGITS = re.compile(r"\D")


def _tail(number: str, n: int = 4) -> str:
    """Last n digits of a phone number — country-prefix agnostic."""
    return _DIGITS.sub("", str(number or ""))[-n:]


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

    def _ping_node(self) -> str:
        return f"{self.db_url}/otps-ping/{self.secret}/{self.mid}.json"

    def _pong_node(self) -> str:
        return f"{self.db_url}/otps-pong/{self.secret}/{self.mid}.json"

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
                await self._poll_ping()
            except Exception as exc:
                logger.warning("otp channel error: %s", exc)
            await asyncio.sleep(self.poll_s)

    async def _poll_ping(self):
        """APK 'test connection' writes here; we answer on the pong node —
        a real round-trip proves phone→firebase→machine→firebase→phone."""
        r = await self._http.get(self._ping_node())
        if r.status_code != 200 or r.text in ("null", ""):
            return
        await self._http.delete(self._ping_node())
        await self._http.put(self._pong_node(),
                             content='"pong"')

    def _phone_ok(self, apk_phone: str) -> bool:
        """Bind the code to the applicant being booked — the APK sends the
        client's number; we accept only if its tail matches the current
        applicant's stored phone. Empty on either side can't bind, so we
        accept rather than silently break the single-phone case."""
        applicant = self.monitor.queue.current()
        want = _tail(((applicant or {}).get("data") or {}).get("phone"))
        got = _tail(apk_phone)
        return not want or not got or want == got

    async def _poll(self):
        r = await self._http.get(self._node())
        if r.status_code != 200 or r.text in ("null", ""):
            return
        # payload: {"code":"123456","phone":"+92…"} — bare "123456" tolerated
        code, apk_phone = None, ""
        m = _CODE.match(r.text)
        if m:
            code = m.group(1)
        else:
            try:
                obj = r.json()
                code = str(obj.get("code", "")).strip()
                apk_phone = str(obj.get("phone", "")).strip()
            except Exception:
                return
        if not code or not code.isdigit() or not (4 <= len(code) <= 8):
            return
        # consume first so a second poll can't re-enter a stale code
        await self._http.delete(self._node())
        if not self._phone_ok(apk_phone):
            applicant = self.monitor.queue.current() or {}
            self.monitor._say(
                "otp_mismatch", source="apk",
                error=f"code from a different phone than "
                      f"{(applicant.get('data') or {}).get('display', '?')}"
                      " — rejected")
            logger.warning("otp channel: phone mismatch — code rejected")
            return
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

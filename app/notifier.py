"""Resend email client."""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"


class Notifier:
    def __init__(self, api_key: str, from_addr: str, to: list[str], dry_run: bool = False):
        self.api_key = api_key
        self.from_addr = from_addr
        self.to = to
        self.dry_run = dry_run
        self._client = httpx.AsyncClient(timeout=30)

    def build_subject(self, slot: dict) -> str:
        cat = slot.get("category", "unknown")
        loc = slot.get("location", "unknown")
        date = slot.get("date", "unknown")
        return f"🟢 NEW SLOT: {cat} @ {loc} — {date}"

    def build_body(self, slot: dict) -> str:
        stanza = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111">
<h2>🟢 New appointment slot detected</h2>
<table style="border-collapse:collapse">
<tr><td style="padding:3px 12px 3px 0"><b>Profile</b></td><td>{slot['profile']}</td></tr>
<tr><td style="padding:3px 12px 3px 0"><b>Category</b></td><td>{slot['category']}</td></tr>
<tr><td style="padding:3px 12px 3px 0"><b>VAC location</b></td><td>{slot['location']}</td></tr>
<tr><td style="padding:3px 12px 3px 0"><b>Date</b></td><td>{slot['date']}</td></tr>
<tr><td style="padding:3px 12px 3px 0"><b>Time</b></td><td>{slot.get('time') or 'Any / not specified'}</td></tr>
</table>
<br/>
<p><a href="{slot.get('booking_url', '')}" style="background:#1570ef;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;display:inline-block">Open booking page</a></p>
<p style="color:#666;font-size:12px">Slot may already be taken — it is first-come, first-served.
This alert is for <b>{slot['profile']}</b>. No automatic booking has been performed.</p>
</body></html>"""
        return stanza

    async def send(self, slot: dict) -> bool:
        subject = self.build_subject(slot)
        body = self.build_body(slot)
        recipients = slot.get("email") or self.to
        if not recipients:
            logger.warning("no recipient for slot alert; skipping")
            return False
        if self.dry_run:
            logger.info("[dry-run] would send to %s: %s", recipients, subject)
            return True
        return await self._post(subject, body, recipients)

    async def _post(self, subject: str, body: str, recipients: list[str]) -> bool:
        payload = {
            "from": self.from_addr,
            "to": recipients,
            "subject": subject,
            "html": body,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in (1, 2, 3):
            try:
                resp = await self._client.post(RESEND_API, json=payload, headers=headers)
                if resp.status_code < 300:
                    logger.info("alert sent: %s (%d)", subject, resp.status_code)
                    return True
                logger.warning("resend error %s: %s", resp.status_code, resp.text[:300])
            except httpx.HTTPError as e:
                logger.warning("resend network error (attempt %d): %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(3 * attempt)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()
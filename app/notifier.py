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

    def build_body(self, slot: dict, auto_book: bool = False) -> str:
        disclaimer = (
            "Automatic booking is enabled for this profile: the crawler will claim the slot "
            "and alarm you to finish the form."
            if auto_book
            else "No automatic booking has been performed."
        )
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
This alert is for <b>{slot['profile']}</b>. {disclaimer}</p>
</body></html>"""
        return stanza

    async def send(self, slot: dict, auto_book: bool = False) -> bool:
        subject = self.build_subject(slot)
        body = self.build_body(slot, auto_book=auto_book)
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

    # ── booking result emails ────────────────────────────────────────────

    BOOKING_HEADLINES = {
        "needs_human": "🔔 SLOT CLAIMED — finish the form now",
        "booked": "✅ BOOKED",
        "slot_gone": "⚪ Slot was taken by someone else",
        "failed": "⚠️ Booking failed",
        "aborted_unknown_state": "⚠️ Booking needs checking by hand",
        "dry_run": "🧪 Dry run — reserve was not clicked",
        "rate_limited": "⏸ Rate limited",
        "skipped_duplicate": "⏭ Skipped (duplicate)",
    }

    def build_booking_subject(self, result: dict) -> str:
        slot = result.get("slot") or {}
        status = str(result.get("status"))
        headline = self.BOOKING_HEADLINES.get(status, f"Booking: {status}")
        return f"{headline} — {slot.get('category')} @ {slot.get('location')} {slot.get('date')}"

    def build_booking_body(self, result: dict) -> str:
        slot = result.get("slot") or {}
        status = str(result.get("status"))
        rows = [
            ("Profile", result.get("profile")),
            ("Status", status),
            ("Stage", result.get("stage")),
            ("Category", slot.get("category")),
            ("VAC location", slot.get("location")),
            ("Date", slot.get("date")),
            ("Time", slot.get("time") or "Any / not specified"),
            ("Confirmation", result.get("confirmation_ref") or "—"),
            ("Evidence", result.get("evidence_path") or "—"),
        ]
        if result.get("error"):
            rows.append(("Detail", result.get("error")))
        table = "".join(
            f'<tr><td style="padding:3px 12px 3px 0"><b>{k}</b></td><td>{v}</td></tr>'
            for k, v in rows
        )
        if status == "needs_human":
            action = (
                '<p style="font-weight:700;color:#b42318">The slot is claimed. Go to the machine '
                "running the crawler and finish the applicant form in the open browser window "
                "before the hold expires.</p>"
            )
        elif status == "booked":
            action = '<p style="color:#027a48">Nothing further to do.</p>'
        elif status == "aborted_unknown_state":
            action = (
                '<p style="font-weight:700;color:#b42318">The slot may or may not be held. Check '
                "the portal by hand before trying again — do not re-book blindly.</p>"
            )
        else:
            action = ""
        return (
            '<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111">'
            f"<h2>{self.BOOKING_HEADLINES.get(status, 'Booking update')}</h2>"
            f'<table style="border-collapse:collapse">{table}</table>{action}</body></html>'
        )

    def notify_booking(self, result: dict) -> None:
        """Queue a booking-result email without blocking the caller."""
        slot = result.get("slot") or {}
        recipients = slot.get("email") or self.to
        if not recipients:
            logger.warning("no recipient for booking email (%s)", result.get("status"))
            return
        if self.dry_run:
            logger.info("[dry-run] would email booking result: %s", result.get("status"))
            return
        coro = self._send_booking(result, recipients)
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            logger.warning("no running loop; booking email not sent")

    async def _send_booking(self, result: dict, recipients: list[str]) -> bool:
        return await self._post(
            self.build_booking_subject(result), self.build_booking_body(result), recipients
        )

    async def aclose(self) -> None:
        await self._client.aclose()
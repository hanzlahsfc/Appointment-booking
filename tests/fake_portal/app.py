"""A miniature stand-in for the GVCW portal, for offline testing.

It mirrors the shapes the real site uses so the booking agent can be exercised
end to end without touching a live visa portal:

  * a Drupal-style login form (#edit-name / #edit-pass / .form-submit)
  * a slot picker (select[name=vac], select[name=category], .slot-row rows)
  * a reserve step that consumes one-time anti-forgery tokens (form_build_id /
    form_token), exactly like Drupal's form API
  * a confirmation page carrying a reference number

It also models the race that actually matters: reserving REMOVES the slot, so a
second attempt at the same date/time finds nothing (that is `slot_gone`).

Form bodies are parsed by hand rather than with fastapi's Form(), so the mock
needs no python-multipart dependency.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

USERNAME = "recon@example.com"
PASSWORD = "s3cret"

VACS = ["lahore", "islamabad"]
CATEGORIES = ["national_d_employment", "national_d_seasonal"]


@dataclass
class PortalState:
    """Mutable portal state; tests drive it directly."""

    slots: list[dict] = field(default_factory=list)
    sessions: dict[str, dict] = field(default_factory=dict)  # sid -> issued token
    held: list[dict] = field(default_factory=list)
    bookings: list[dict] = field(default_factory=list)
    reserve_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_slot(self, date: str, time: str, vac: str = "lahore", category: str = "national_d_employment") -> None:
        self.slots.append({"date": date, "time": time, "vac": vac, "category": category})

    def reset(self) -> None:
        self.slots.clear()
        self.sessions.clear()
        self.held.clear()
        self.bookings.clear()
        self.reserve_calls = 0


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><title>{t}</title></head>"
        '<body style="font-family:Arial">{t}<hr>{b}</body></html>'.format(t=title, b=body)
    )


def create_app(state: PortalState | None = None) -> FastAPI:
    portal = state or PortalState()
    app = FastAPI()
    app.state.portal = portal

    def _new_token() -> str:
        return "tok-" + secrets.token_hex(4)

    def _session(request: Request) -> str | None:
        return request.cookies.get("sid")

    def _valid_token(request: Request, form: dict) -> bool:
        sid = _session(request)
        if not sid or sid not in portal.sessions:
            return False
        issued = portal.sessions[sid].get("token")
        return bool(issued) and form.get("form_token") == issued

    async def _form(request: Request) -> dict:
        raw = (await request.body()).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # ── auth ─────────────────────────────────────────────────────────────

    @app.get("/login/appointments/en", response_class=HTMLResponse)
    async def login_page() -> HTMLResponse:
        return _page(
            "Sign in to GVCW (Enter Username or E-Mail &amp; Password)",
            '<form method="post" action="/login">'
            '<input id="edit-name" name="name" type="text" placeholder="Username or E-Mail">'
            '<input id="edit-pass" name="pass" type="password" placeholder="Password">'
            '<button type="submit" class="form-submit">Sign in</button>'
            "</form>",
        )

    @app.post("/login")
    async def do_login(request: Request) -> RedirectResponse:
        form = await _form(request)
        if form.get("name") != USERNAME or form.get("pass") != PASSWORD:
            return RedirectResponse("/login/appointments/en?error=1", status_code=303)
        sid = "sid-" + secrets.token_hex(6)
        portal.sessions[sid] = {"token": _new_token()}
        response = RedirectResponse("/en/account", status_code=303)
        response.set_cookie("sid", sid, httponly=True)
        return response

    @app.get("/en/account", response_class=HTMLResponse)
    async def account(request: Request) -> HTMLResponse:
        if not _session(request):
            return RedirectResponse("/login/appointments/en", status_code=303)  # type: ignore[return-value]
        return _page("Account", "<p>Signed in.</p>")

    # ── slot picker ──────────────────────────────────────────────────────

    @app.get("/en/appointments", response_class=HTMLResponse)
    async def appointments(request: Request) -> HTMLResponse:
        sid = _session(request)
        if not sid or sid not in portal.sessions:
            return RedirectResponse("/login/appointments/en", status_code=303)  # type: ignore[return-value]

        token = _new_token()
        portal.sessions[sid]["token"] = token

        vac_options = "".join(
            '<option value="{v}">{v}</option>'.format(v=v) for v in VACS
        )
        cat_options = "".join(
            '<option value="{c}">{c}</option>'.format(c=c) for c in CATEGORIES
        )
        rows = "".join(
            '<div class="slot-row">{d} {t} '
            '<form class="reserve-form" method="post" action="/reserve">'
            '<input type="hidden" name="date" value="{d}">'
            '<input type="hidden" name="time" value="{t}">'
            '<input type="hidden" name="form_build_id" value="build-{n}">'
            '<input type="hidden" name="form_token" value="{tok}">'
            '<button type="submit" class="reserve">Book this slot</button>'
            "</form></div>".format(d=s["date"], t=s["time"], n=i, tok=token)
            for i, s in enumerate(portal.slots)
        ) or '<p class="no-slots">No appointments available.</p>'

        return _page(
            "Appointments",
            '<form method="get" action="/en/appointments">'
            '<select name="vac">{v}</select><select name="category">{c}</select>'
            '<button type="submit" id="apply">Apply</button></form>'
            '<div id="slot-list">{rows}</div>'.format(v=vac_options, c=cat_options, rows=rows),
        )

    # ── reserve / submit ─────────────────────────────────────────────────

    @app.post("/reserve", response_class=HTMLResponse)
    async def reserve(request: Request) -> HTMLResponse:
        portal.reserve_calls += 1
        form = await _form(request)
        sid = _session(request)

        if not _valid_token(request, form):
            return _page("Session expired", '<p id="error">Token rejected. Start again.</p>')

        target = {"date": form.get("date"), "time": form.get("time")}
        with portal.lock:
            match = next(
                (s for s in portal.slots
                 if s["date"] == target["date"] and s["time"] == target["time"]),
                None,
            )
            if match is None:
                return _page(
                    "Slot unavailable",
                    '<p id="slot-gone">That slot has just been taken. Please pick another.</p>',
                )
            portal.slots.remove(match)
            portal.held.append(dict(match))

        token = _new_token()
        if sid:
            portal.sessions[sid]["token"] = token

        return _page(
            "Applicant details",
            '<p id="hold-note">Slot held for you. Complete the details to confirm.</p>'
            '<form method="post" action="/submit">'
            '<input id="edit-passport" name="passport" type="text" placeholder="Passport number">'
            '<input id="edit-fullname" name="fullname" type="text" placeholder="Full name">'
            '<input type="hidden" name="date" value="{d}">'
            '<input type="hidden" name="time" value="{t}">'
            '<input type="hidden" name="form_token" value="{tok}">'
            '<button type="submit" id="edit-submit">Confirm appointment</button>'
            "</form>".format(d=target["date"], t=target["time"] or "", tok=token),
        )

    @app.post("/submit", response_class=HTMLResponse)
    async def submit(request: Request) -> HTMLResponse:
        form = await _form(request)
        if not _valid_token(request, form):
            return _page("Session expired", '<p id="error">Token rejected.</p>')

        reference = "GVCW-" + secrets.token_hex(3).upper()
        with portal.lock:
            portal.bookings.append(
                {
                    "date": form.get("date"),
                    "time": form.get("time"),
                    "passport": form.get("passport"),
                    "fullname": form.get("fullname"),
                    "reference": reference,
                }
            )
        return _page(
            "Appointment confirmed",
            '<h2 id="confirmation">{ref}</h2><p>Keep this reference.</p>'.format(ref=reference),
        )

    return app


if __name__ == "__main__":  # manual poking: python tests/fake_portal/app.py
    import uvicorn

    seed = PortalState()
    seed.add_slot("2026-12-01", "09:00")
    seed.add_slot("2026-12-02", "10:30")
    uvicorn.run(create_app(seed), host="127.0.0.1", port=8099)

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
    # gvcw-faithful replica state (search-driven picker, OTP, result page)
    gvcw_slots: list[dict] = field(default_factory=list)
    gvcw_bookings: list[dict] = field(default_factory=list)
    gvcw_otp: str = "246810"
    gvcw_fail_next_submit: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_slot(self, date: str, time: str, vac: str = "lahore", category: str = "national_d_employment") -> None:
        self.slots.append({"date": date, "time": time, "vac": vac, "category": category})

    def add_gvcw_slot(self, date_dmy: str, time_hm: str, category: str = "26",
                      available: int = 1, selectable: bool = True) -> dict:
        slot = {
            "id": len(self.gvcw_slots) + 1000,
            "periodid": 14816,
            "date": date_dmy,
            "starttime": time_hm,
            "endtime": "",
            "numofavailableslots": available,
            "isavailable": available > 0,
            "isselectable": selectable and available > 0,
            "related": [],
            "category": str(category),
        }
        self.gvcw_slots.append(slot)
        return slot

    def reset(self) -> None:
        self.slots.clear()
        self.sessions.clear()
        self.held.clear()
        self.bookings.clear()
        self.reserve_calls = 0
        self.gvcw_slots.clear()
        self.gvcw_bookings.clear()
        self.gvcw_fail_next_submit = None


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

    @app.get("/gvcw-login", response_class=HTMLResponse)
    async def gvcw_login_page() -> HTMLResponse:
        """Real-portal-shaped login (the real form uses #username/#password/
        #btn-login, not the legacy Drupal ids)."""
        return _page(
            "Sign in to GVCW (Enter Username or E-mail &amp; Password)",
            '<form method="post" action="/login">'
            '<input id="username" name="name" type="text">'
            '<input id="password" name="pass" type="password">'
            '<input id="destination" type="hidden" value="/appointments/add">'
            '<button type="button" id="btn-login" '
            "onclick=\"this.form.method='post';this.form.submit()\">Sign in</button>"
            "</form>",
        )

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

    # ── gvcw-faithful replica (search-driven picker + OTP + result page) ──
    #
    # Mirrors the real pk-gr-services flow as decoded from app.js: the /add
    # form holds #type/#datefrom/#btn-search; Search issues
    # PUT /api/v1/periodslot/slots and renders .appointment_slot cells into
    # #result (client-side); picking a cell only marks it selected; the real
    # reservation is #btn-new-appointment behind OTP + consent checkbox;
    # success lands on /appointments/result/{ref}.

    GVCW_PAGE = """<!doctype html><html><head><title>Visa | My Appointments | Add</title>
<style>.hidden{display:none}.appointment_slot{padding:6px;border:1px solid #ccc;margin:2px;display:inline-block}
.appointment_slot_disabled{opacity:.4}.appointment_slot_notselectable{opacity:.6}
.appointment_slot_selected{background:#4a9;border-color:#282}.alertify .ajs-error{color:#a00}</style></head>
<body>
<h3 data-name="appointmentEditId"></h3>
<form action="/appointments/add" method="get">
<input type="hidden" id="no-results" value="No results found">
<input type="hidden" id="otpuser" value="User{id=1, username=mock, password=x}">
<input type="hidden" id="vpm" value="true"><input type="hidden" id="vac" value="138">
<select id="type" class="form-control"><option value="0">Submission Schengen Visa (Short term – Type C)</option>
<option value="2">National visa (Long term - type D)</option><option value="26">Long-Term Type D (Seasonal/Dependent Employment)</option></select>
<div id="travelpurposesDiv" class="hidden"><select id="travelpurposes"><option value=""></option><option value="11">Seasonal work</option></select></div>
<select id="bookingfor"><option value="0">Individual</option><option value="1">Group (Family/Traveler)</option></select>
<div id="membersDiv"><select id="members"><option value="1">1</option><option value="2">2</option></select></div>
<select id="appointmentmethod"><option value="1">Same time</option><option value="4">Select one by one</option></select>
<input id="datefrom" type="text" placeholder="DD/MM/YYYY">
<button type="button" id="btn-search">Search</button>
<input id="selectedtime" type="hidden"><input id="init_selecteddate" type="hidden">
<input id="init_selectedtime" type="hidden"><input id="init_periodslotid" type="hidden">
<div id="resultDiv"></div><div id="resultMessage" class="hidden">No results found</div>
<div id="appointment_box" class="hidden">
<input id="gp_surname"><input id="gp_firstname"><input id="gp_dateofbirth">
<input id="gp_passportnumber"><input id="gp_traveldocumentvaliduntil">
<select id="gp_gender"><option value="1">FEMALE</option><option value="2">MALE</option></select>
<select id="gp_nationality"><option value="146">PAKISTAN</option></select>
<input id="gp_periodslotid" type="hidden"><input id="ex_periodslotid" type="hidden">
<input id="ind_email"><select id="phonenumberprefix"><option value="40">PAKISTAN +92</option></select><input id="ind_phonenumber">
<span id="gp_time"></span>
<div id="btn-onetimepassword-wrap"><button type="button" id="btn-onetimepassword">Request OTP code (via Mobile)</button></div>
<div id="onetimepassword-wrap" class="hidden"><input id="onetimepassword"></div>
<input type="checkbox" id="submitinfo" name="submitinfo">
<div id="recaptcha" class="hidden"></div><textarea id="g-recaptcha-response" class="hidden"></textarea>
<button type="button" id="btn-new-appointment" class="hidden">Book your appointment</button>
<div id="alertmsg" class="hidden">Please select a slot first</div>
</div>
<div class="alertify"><div class="ajs-message ajs-error hidden" id="ajerr"></div></div>
</form>
<script>
function $(id){return document.getElementById(id)}
function err(m){var e=$('ajerr');e.textContent=m;e.classList.remove('hidden');e.style.display='block';}
window.selectTime=function(pid,time){
  var el=$('t_'+pid);var already=el.classList.contains('appointment_slot_selected');
  document.querySelectorAll('div[id^=t_]').forEach(function(d){d.classList.remove('appointment_slot_selected')});
  if(already){$('selectedtime').value='';$('gp_periodslotid').value='';$('gp_time').textContent='';return;}
  el.classList.add('appointment_slot_selected');$('selectedtime').value=time;$('gp_periodslotid').value=pid;
  $('gp_time').textContent=time;$('gp_time').classList.add('appointment_slot_reserved');};
function createAppointments(slots){
  var html='',count=0,row='';
  slots.forEach(function(s){var cls=s.isavailable?(s.isselectable?'appointment_slot_enabled':'appointment_slot_notselectable'):'appointment_slot_disabled';
    var oc=s.isavailable&&s.isselectable?' onclick="selectTime('+s.id+',\\''+s.starttime+'\\')"':'';
    row+='<td><div id="t_'+s.id+'" slot-time="'+s.starttime+'" class="appointment_slot '+cls+'"'+oc+'>'+s.starttime+'</div></td>';
    if(++count==4){html+='<tr>'+row+'</tr>';row='';count=0;}});
  if(row){html+='<tr>'+row+'</tr>';}
  var old=$('result');if(old)old.remove();
  $('resultDiv').innerHTML='<table id="result">'+html+'</table>';
  $('resultMessage').classList.add('hidden');$('appointment_box').classList.remove('hidden');};
$('btn-search').onclick=async function(){
  $('ajerr').classList.add('hidden');
  var body=JSON.stringify({datefrom:$('datefrom').value,type:+($('type').value),bookingfor:+($('bookingfor').value),
    members:+($('members').value),method:+($('appointmentmethod').value),travelpurposes:-1,
    howmanyapplicantsareunder12:0,appointmentId:'undefined',id:0,vac:{id:+($('vac').value)}});
  var r=await fetch('/api/v1/periodslot/slots',{method:'PUT',headers:{'Content-Type':'application/json'},body:body});
  var data=await r.json();
  if(data.code==='SUCCESS'&&data.returnobject.slots.length){createAppointments(data.returnobject.slots);}
  else{$('resultMessage').classList.remove('hidden');var t=$('result');if(t)t.remove();}};
$('btn-onetimepassword').onclick=async function(){
  if(!$('datefrom').value||!$('selectedtime').value){err($('alertmsg').textContent);return;}
  var r=await fetch('/api/v1/onetimepassword/sendOtpBookAppointment/'+$('ind_phonenumber').value+'/'+$('phonenumberprefix').value,
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({datefrom:$('datefrom').value,selectedtime:$('selectedtime').value})});
  var data=await r.json();
  if(data.code==='SUCCESS'){$('onetimepassword-wrap').classList.remove('hidden');$('recaptcha').classList.remove('hidden');
    $('btn-new-appointment').classList.remove('hidden');$('btn-onetimepassword-wrap').classList.add('hidden');}
  else err(data.message||'otp failed');};
$('btn-new-appointment').onclick=async function(){
  if(!$('selectedtime').value||!$('gp_periodslotid').value||!$('submitinfo').checked){err('Check the required checkbox and pick a slot');return;}
  var r=await fetch('/api/v1/appointments/book',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({selectedtime:$('selectedtime').value,periodslotid:$('gp_periodslotid').value,
      onetimepassword:$('onetimepassword').value,submitinfo:$('submitinfo').checked,
      surname:$('gp_surname').value,firstname:$('gp_firstname').value,passport:$('gp_passportnumber').value})});
  var data=await r.json();
  if(data.code==='SUCCESS'){location.href='/appointments/result/'+data.returnobject;}else err(data.message||'booking failed');};
</script></body></html>"""

    @app.get("/appointments/add", response_class=HTMLResponse)
    async def gvcw_add(request: Request) -> HTMLResponse:
        if not _session(request) or _session(request) not in portal.sessions:
            return RedirectResponse("/login/appointments/en", status_code=303)  # type: ignore[return-value]
        return HTMLResponse(GVCW_PAGE)

    @app.put("/api/v1/periodslot/slots")
    async def gvcw_slots(request: Request):
        if not _session(request) or _session(request) not in portal.sessions:
            return {"code": "INVALID", "message": "session"}
        body = await request.json()
        date_dmy = str(body.get("datefrom", ""))
        typ = str(body.get("type", ""))
        with portal.lock:
            found = [
                s for s in portal.gvcw_slots
                if s["date"] == date_dmy and s["category"] == typ
            ]
        return {
            "code": "SUCCESS",
            "message": "",
            "returnobject": {
                "slots": [
                    {k: s[k] for k in (
                        "id", "periodid", "date", "starttime", "endtime",
                        "numofavailableslots", "isavailable", "isselectable",
                        "related")}
                    for s in found
                ]
            },
        }

    @app.post("/api/v1/onetimepassword/sendOtpBookAppointment/{phone}/{prefix}")
    async def gvcw_otp(phone: str, prefix: str, request: Request):
        if not _session(request) or _session(request) not in portal.sessions:
            return {"code": "INVALID", "message": "session"}
        body = await request.json()
        if not body.get("datefrom") or not body.get("selectedtime"):
            return {"code": "INVALID", "message": "Pick a date and time first"}
        portal.sessions[_session(request)]["otp"] = portal.gvcw_otp
        return {"code": "SUCCESS", "message": "OTP sent"}

    @app.post("/api/v1/appointments/book")
    async def gvcw_book(request: Request):
        sid = _session(request)
        if not sid or sid not in portal.sessions:
            return {"code": "INVALID", "message": "session"}
        body = await request.json()
        if portal.gvcw_fail_next_submit:
            msg = portal.gvcw_fail_next_submit
            portal.gvcw_fail_next_submit = None
            return {"code": "FAIL", "message": msg}
        if not body.get("selectedtime") or not body.get("periodslotid"):
            return {"code": "INVALID", "message": "No slot selected"}
        if not body.get("submitinfo"):
            return {"code": "INVALID", "message": "Consent checkbox required"}
        if body.get("onetimepassword") != portal.sessions[sid].get("otp"):
            return {"code": "INVALID", "message": "Wrong one-time password"}
        pid = int(body["periodslotid"])
        with portal.lock:
            slot = next((s for s in portal.gvcw_slots if s["id"] == pid), None)
            if slot is None or slot["numofavailableslots"] < 1:
                return {"code": "FAIL", "message": "The selected slot is no longer available"}
            slot["numofavailableslots"] -= 1
            if slot["numofavailableslots"] < 1:
                slot["isavailable"] = False
                slot["isselectable"] = False
            ref = "GVCW-" + secrets.token_hex(3).upper()
            portal.gvcw_bookings.append(
                {"ref": ref, "slot": dict(slot),
                 "surname": body.get("surname"),
                 "firstname": body.get("firstname"),
                 "passport": body.get("passport")}
            )
        return {"code": "SUCCESS", "message": "", "returnobject": ref}

    @app.get("/appointments/result/{ref}", response_class=HTMLResponse)
    async def gvcw_result(ref: str, request: Request) -> HTMLResponse:
        if not _session(request) or _session(request) not in portal.sessions:
            return RedirectResponse("/login/appointments/en", status_code=303)  # type: ignore[return-value]
        return _page(
            "Appointment result",
            '<h2 id="confirmation">Appointment confirmed</h2>'
            '<p class="ref">Reference: {r}</p>'.format(r=ref),
        )

    @app.post("/__admin__/gvcw-slots")
    async def admin_gvcw_slot(request: Request):
        body = await request.json()
        slot = portal.add_gvcw_slot(
            str(body["date"]), str(body["time"]),
            category=str(body.get("category", "26")),
            available=int(body.get("available", 1)),
            selectable=bool(body.get("selectable", True)),
        )
        return {"ok": True, "id": slot["id"]}

    @app.post("/__admin__/gvcw-fail")
    async def admin_gvcw_fail(request: Request):
        body = await request.json()
        portal.gvcw_fail_next_submit = str(body.get("message", "The selected slot is no longer available"))
        return {"ok": True}

    @app.get("/__admin__/gvcw-state")
    async def admin_gvcw_state():
        return {"slots": list(portal.gvcw_slots),
                "bookings": list(portal.gvcw_bookings),
                "otp": portal.gvcw_otp}

    # ── test-only admin ───────────────────────────────────────────────────
    # These endpoints only exist because this process IS the fake portal; they
    # let the hosted-demo and e2e tests seed/inspect slots without touching a
    # database. The fake portal only ever runs on loopback.

    @app.post("/__admin__/slots")
    async def admin_add_slot(request: Request):
        body = await request.json()
        portal.add_slot(
            str(body["date"]), str(body["time"]),
            vac=str(body.get("vac", "lahore")),
            category=str(body.get("category", "national_d_employment")),
        )
        return {"ok": True, "slots": len(portal.slots)}

    @app.post("/__admin__/reset")
    async def admin_reset():
        portal.reset()
        return {"ok": True}

    @app.get("/__admin__/state")
    async def admin_state():
        return {
            "slots": list(portal.slots),
            "held": list(portal.held),
            "bookings": list(portal.bookings),
            "reserve_calls": portal.reserve_calls,
        }

    return app


if __name__ == "__main__":  # manual poking: python tests/fake_portal/app.py
    import uvicorn

    seed = PortalState()
    seed.add_slot("2026-12-01", "09:00")
    seed.add_slot("2026-12-02", "10:30")
    uvicorn.run(create_app(seed), host="127.0.0.1", port=8099)

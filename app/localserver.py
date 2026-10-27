"""Local app server — single-operator dashboard + API for the queue app.

One localhost server serving a self-contained dashboard and a token-scoped
JSON API. Pairing: the server issues a random token at startup (printed to
the console and saved to data/app-token); every API call must carry it.
The portal tab is chosen explicitly by its bridge tab id and must sit on
the configured portal origin — nothing else is ever touched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

from .bridge import HUB, BridgeClient, BridgeError, BridgeProtocolError
from .localapp import LocalMonitor
from .queue import ApplicantQueue

logger = logging.getLogger(__name__)

DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Appointment Booker — Local</title>
<style>
body{font-family:system-ui;background:#0f1420;color:#dfe7f0;margin:0;padding:16px}
h1{font-size:20px}h2{font-size:15px;margin:12px 0 6px}
.card{background:#172033;border:1px solid #2a3550;border-radius:8px;padding:12px;margin-bottom:10px}
button{background:#2b5cff;color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer;margin:2px}
button.ghost{background:transparent;border:1px solid #4a5a80;color:#9fb3dd}
button.danger{background:#b3331d}button:disabled{opacity:.4}
input,select{background:#0f1420;color:#dfe7f0;border:1px solid #2a3550;border-radius:5px;padding:5px;margin:2px;max-width:180px}
table{width:100%;border-collapse:collapse}td,th{padding:4px 6px;border-bottom:1px solid #22304d;text-align:left;font-size:13px}
.pill{display:inline-block;border-radius:10px;padding:1px 8px;font-size:11px;background:#2a3550}
.pill.booked{background:#1e6b34}.pill.selected_awaiting_human,.pill.outcome_unknown{background:#9a6a16}
.pill.failed{background:#8a2626}.pill.monitoring{background:#2b5cff}
.muted{color:#8b98b5;font-size:12px}#banner{padding:8px;border-radius:6px;margin-bottom:10px;display:none}
.warn{background:#4a3412;border:1px solid #9a6a16}.ok{background:#123a1e;border:1px solid #1e6b34}
.err{background:#3a1512;border:1px solid #8a2626}
#feed{max-height:180px;overflow:auto;font-size:12px}
</style></head><body>
<h1>Appointment Booker — local queue</h1>
<div id="banner"></div>
<div class="card"><h2>Status</h2><div id="status" class="muted">—</div>
<div id="meta" class="muted"></div>
<div style="margin-top:6px">
<button onclick="ctl('start')">Start</button>
<button onclick="ctl('pause')" class="ghost">Pause</button>
<button onclick="ctl('resume')" class="ghost">Resume</button>
<button onclick="ctl('stop')" class="ghost">Stop</button>
<button id="snd" onclick="enableSound()" class="ghost">Enable sound</button>
<button id="sndt" onclick="beep()" class="ghost" style="display:none">Test</button>
<button onclick="ack()">Acknowledge</button></div></div>
<div class="card"><h2>Pair portal tab</h2>
<div class="muted">Open the signed-in portal page in Chrome, then pair its bridge tab id.</div>
<input id="tabid" placeholder="tab id (e.g. 413653551)"><button onclick="pair()">Pair</button>
<div id="pair-status" class="muted"></div></div>
<div class="card"><h2>Monitor config</h2>
Category <select id="cat"><option value="26">Seasonal/Dependent Employment (26)</option><option value="2">National visa D (2)</option></select>
From <input id="df" placeholder="DD/MM/YYYY"> To <input id="dt" placeholder="DD/MM/YYYY">
<div class="muted">Centre is fixed by the portal account — shown on the page, never set here.</div></div>
<div class="card"><h2>Applicant queue</h2>
<div id="newapp">
<input id="an" placeholder="Full name (label)">
<input id="as" placeholder="surname"><input id="af" placeholder="firstname">
<input id="ad" placeholder="DOB DD/MM/YYYY"><input id="ap" placeholder="passport">
<input id="av" placeholder="doc valid until DD/MM/YYYY">
<input id="ae" placeholder="email"><input id="ah" placeholder="phone">
<button onclick="addApp()">Add</button></div>
<table><tr><th>#</th><th>name</th><th>status</th><th>note</th><th></th></tr>
<tbody id="q"></tbody></table></div>
<div class="card" id="bookbox" style="display:none"><h2>Held booking — finish it</h2>
<div id="book-info"></div>
<button onclick="otpReq()">Request SMS code</button>
<input id="otpc" placeholder="SMS code" maxlength="8">
<button onclick="otpSend()" class="ghost">Enter code</button>
<button onclick="confirmBook()" class="danger">Confirm and book</button>
<div class="muted">Solve the captcha in Chrome first. Confirm submits the real form — never retried automatically.</div></div>
<div class="card"><h2>Log</h2><div id="feed"></div></div>
<script>
const T = new URLSearchParams(location.search).get('token') || localStorage.ab_token || '';
localStorage.ab_token = T;
let actx=null, alarmT=null;
function enableSound(){actx=new (window.AudioContext||webkitAudioContext)();actx.resume();snd.style.display='none';sndt.style.display='';}
function beep(){if(!actx)return;const o=actx.createOscillator(),g=actx.createGain();o.connect(g);g.connect(actx.destination);o.frequency.value=880;g.gain.value=.12;o.start();o.stop(actx.currentTime+.35);}
function alarm(){if(alarmT)return;alarmT=setInterval(beep,1200);beep();}
function ack(){clearInterval(alarmT);alarmT=null;}
async function api(p,o){const r=await fetch(p,{...o,headers:{...(o&&o.headers||{}),'Authorization':'Bearer '+T,'Content-Type':'application/json'}});return r;}
function banner(t,k){const b=document.getElementById('banner');b.textContent=t;b.className=k||'warn';b.style.display=t?'block':'none';}
async function refresh(){
 const r=await api('/api/state');if(r.status!==200){banner('dashboard token missing — open START.bat output URL', 'err');return;}
 const d=await r.json();
 document.getElementById('status').innerHTML='status <b>'+d.status+'</b>'+(d.paused?' (paused)':'')+' · tab '+ (d.tab_id||'—');
 const s=d.feed&&d.feed.stats||{};
 document.getElementById('meta').textContent='searches '+(s.searches||0)+' · last check '+(s.last_check?new Date(s.last_check*1000).toLocaleTimeString():'—')+' · latency '+(s.latency_ms||'—')+'ms';
 const cur=d.current;let hold=d.status==='selected_awaiting_human'||d.status==='outcome_unknown'||d.status==='submitting';
 document.getElementById('bookbox').style.display=hold?'block':'none';
 if(hold&&cur)document.getElementById('book-info').innerHTML='Applicant <b>'+esc(cur.display_name)+'</b> — '+esc(cur.note||'');
 if(d.status==='selected_awaiting_human'){alarm();banner('SLOT SELECTED — finish OTP + captcha, then Confirm and book','warn');}
 else if(d.status==='booked'){ack();banner('Booked! reference '+ (d.feed&&d.feed.ref||''),'ok');}
 else if(d.status==='outcome_unknown'){alarm();banner('Submission outcome unclear — CHECK THE PORTAL PAGE before anything','err');}
 const q=document.getElementById('q');q.innerHTML='';
 (d.queue||[]).forEach(function(a){q.innerHTML+='<tr><td>'+a.position+'</td><td>'+esc(a.display_name)+'</td><td><span class="pill '+a.status+'">'+a.status+'</span></td><td>'+esc(a.note||'')+'</td><td><button class="ghost" onclick="mv('+a.id+',-1)">↑</button><button class="ghost" onclick="mv('+a.id+',1)">↓</button><button class="ghost" onclick="del('+a.id+')">✕</button></td></tr>';});
 const f=document.getElementById('feed');f.innerHTML=(d.feed&&d.feed.events||[]).map(e=>'<div>'+new Date(e.ts*1000).toLocaleTimeString()+' — '+esc(e.status)+(e.error?' · '+esc(e.error):'')+(e.date?' · '+e.date:'')+'</div>').join('');
}
function esc(s){return (s||'').toString().replace(/[<>&]/g,'');}
async function pair(){const t=document.getElementById('tabid').value.trim();const r=await api('/api/pair',{method:'POST',body:JSON.stringify({tab_id:+t})});const d=await r.json();document.getElementById('pair-status').textContent=d.ok?'paired ✓':('failed: '+(d.error||''));}
async function ctl(a){await api('/api/monitor/'+a,{method:'POST'});refresh();}
async function addApp(){const data={surname:as.value,firstname:af.value,dateofbirth:ad.value,passportnumber:ap.value,traveldocumentvaliduntil:av.value,email:ae.value,phonenumber:ah.value};const r=await api('/api/applicants',{method:'POST',body:JSON.stringify({display_name:an.value||as.value+' '+af.value,data:data,category:document.getElementById('cat').value,date_from:df.value,date_to:dt.value})});const d=await r.json();if(d.error)banner(d.error,'err');else banner('');refresh();}
async function mv(i,dir){await api('/api/applicants/'+i+'/move',{method:'POST',body:JSON.stringify({direction:dir})});refresh();}
async function del(i){await api('/api/applicants/'+i,{method:'DELETE'});refresh();}
async function otpReq(){const r=await api('/api/booking/otp-request',{method:'POST'});const d=await r.json();banner(d.ok?'OTP requested — check your phone':d.error,'warn');}
async function otpSend(){const r=await api('/api/booking/otp',{method:'POST',body:JSON.stringify({code:otpc.value})});const d=await r.json();banner(d.ok?'code entered — solve captcha, then Confirm':d.error,'warn');}
async function confirmBook(){if(!confirm('Submit the real booking now?'))return;const r=await api('/api/booking/confirm',{method:'POST'});const d=await r.json();banner(d.ok?'Booked! '+d.confirmation_ref:(d.error||'unclear'),d.ok?'ok':'err');refresh();}
setInterval(refresh,3000);refresh();
</script></body></html>"""


def create_app(queue: ApplicantQueue, monitor: LocalMonitor,
               bridge_client, allowed_origin: str, token: str) -> FastAPI:
    app = FastAPI(title="Appointment Booker — Local", docs_url=None,
                  redoc_url=None)

    def authed(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        return auth == f"Bearer {token}"

    def _401():
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # ── extension bridge socket ──────────────────────────────────────────
    # The bundled extension connects here: ws://127.0.0.1:PORT/bridge?token=T
    # Each socket becomes a BridgeClient registered into the shared HUB —
    # the monitor and API then reach the operator's selected tab through it.
    @app.websocket("/bridge")
    async def bridge_socket(websocket: WebSocket):
        if websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        client = BridgeClient(send_raw=websocket.send_text)
        HUB.register(client)
        try:
            while True:
                raw = await websocket.receive_text()
                await client.handle_message(json.loads(raw))
        except Exception:
            pass
        finally:
            HUB.unregister(client)

    @app.get("/")
    async def index():
        return HTMLResponse(DASH_HTML)

    @app.get("/api/state")
    async def state(request: Request):
        if not authed(request):
            return _401()
        return JSONResponse(monitor.snapshot())

    @app.get("/api/tabs")
    async def tabs(request: Request):
        if not authed(request):
            return _401()
        try:
            client = bridge_client.client()
            tabs = await client.tabs()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"tabs": [
            {"tabId": t.get("tabId") or t.get("id"), "url": t.get("url"),
             "title": t.get("title")}
            for t in tabs]})

    @app.post("/api/pair")
    async def pair(request: Request):
        if not authed(request):
            return _401()
        # The extension popup already selects the tab — pairing here just
        # confirms the extension's current selection is a portal tab and
        # starts driving it. We never ask the extension to switch tabs.
        try:
            client = bridge_client.client()
        except Exception:
            return JSONResponse({"ok": False,
                                 "error": "no extension connected — load it in "
                                 "Chrome and select the portal tab in its popup"})
        try:
            tab_id = client.current_tab()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        try:
            url = await client.evaluate("location.href", timeout=10) or ""
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        if not str(url).startswith(allowed_origin):
            return JSONResponse({"ok": False,
                                 "error": f"selected tab is not on "
                                 f"{allowed_origin}: {url}"})
        monitor.pair(tab_id)
        return JSONResponse({"ok": True, "tab": {"tabId": tab_id, "url": url}})

    @app.post("/api/monitor/{action}")
    async def monitor_ctl(action: str, request: Request):
        if not authed(request):
            return _401()
        if action == "start":
            if monitor._task is None or monitor._task.done():
                monitor._stop.clear()
                monitor.start()
            return JSONResponse({"ok": True})
        if action == "pause":
            monitor.paused = True
            return JSONResponse({"ok": True})
        if action == "resume":
            monitor.paused = False
            return JSONResponse({"ok": True})
        if action == "stop":
            await monitor.stop()
            monitor._task = None
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "unknown action"}, status_code=400)

    @app.post("/api/applicants")
    async def add_applicant(request: Request):
        if not authed(request):
            return _401()
        body = await request.json()
        try:
            row = queue.add(
                body.get("data") or {},
                body.get("display_name") or "applicant",
                category=body.get("category"),
                date_from=body.get("date_from"),
                date_to=body.get("date_to"),
            )
            return JSONResponse({"ok": True, "applicant": {
                k: row[k] for k in ("id", "position", "status", "display_name")}})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.patch("/api/applicants/{app_id}")
    async def edit_applicant(app_id: int, request: Request):
        if not authed(request):
            return _401()
        body = await request.json()
        ok = queue.update(
            app_id, display_name=body.get("display_name"),
            data=body.get("data"), category=body.get("category"),
            date_from=body.get("date_from"), date_to=body.get("date_to"))
        return JSONResponse({"ok": ok})

    @app.delete("/api/applicants/{app_id}")
    async def del_applicant(app_id: int, request: Request):
        if not authed(request):
            return _401()
        return JSONResponse({"ok": queue.remove(app_id)})

    @app.post("/api/applicants/{app_id}/move")
    async def move_applicant(app_id: int, request: Request):
        if not authed(request):
            return _401()
        body = await request.json()
        direction = -1 if int(body.get("direction", 0)) < 0 else 1
        return JSONResponse({"ok": queue.move(app_id, direction)})

    @app.post("/api/booking/otp-request")
    async def otp_req(request: Request):
        if not authed(request):
            return _401()
        return JSONResponse(await monitor.request_otp())

    @app.post("/api/booking/otp")
    async def otp_enter(request: Request):
        if not authed(request):
            return _401()
        body = await request.json()
        return JSONResponse(await monitor.submit_otp(str(body.get("code", ""))))

    @app.post("/api/booking/confirm")
    async def confirm(request: Request):
        if not authed(request):
            return _401()
        return JSONResponse(await monitor.confirm_book())

    return app


def issue_token(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "app-token"
    if p.exists():
        return p.read_text().strip()
    token = "ab-" + secrets.token_hex(8)
    p.write_text(token)
    return token


def run() -> None:
    """Entrypoint for START.bat: `python -m app.localserver`."""
    import webbrowser

    import uvicorn

    from .config import load_config

    cfg = load_config()
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    queue = ApplicantQueue(data_dir / "queue.db")
    allowed = getattr(cfg, "gvcw_origin",
                      "https://pk-gr-services.gvcworld.eu")
    token = issue_token(data_dir)

    async def page_factory():
        # The bundled extension holds the WS socket into HUB and the
        # operator picks the tab in its popup. If nothing is paired we have
        # no page — the monitor waits in "awaiting_pair".
        from .ws_page import WsPage
        try:
            client = HUB.client()
        except Exception:
            client = None
        if client is None:
            raise BridgeError(
                "no extension tab connected — pair one in the extension popup")
        return WsPage(client, [allowed], allow_writes=True)

    monitor = LocalMonitor(queue, page_factory, allowed)
    app = create_app(queue, monitor, HUB, allowed, token)
    port = int(getattr(cfg, "local_port", 8080))
    url = f"http://127.0.0.1:{port}/?token={token}"
    print(f"Appointment Booker — local app")
    print(f"Dashboard: {url}")
    print(f"Portal origin: {allowed} · extension bridge: ws://127.0.0.1:{port}/bridge")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    run()

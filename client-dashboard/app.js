/* Appointment Booker — client dashboard.
 *
 * Talks to the hosted backend over the same origin (served locally now; the
 * build is plain static files so it can move to Vercel unchanged — set
 * window.AB_API_BASE before this script runs to point it at the tunnel).
 *
 * One WebSocket carries everything: screencast frames down, validated input
 * events up. The client only ever sees its own session — the server owns
 * all isolation.
 */
"use strict";

const API = window.AB_API_BASE || "";
const $ = (id) => document.getElementById(id);

/* Bearer token works both same-origin (local server) and cross-origin
 * (Vercel dashboard -> tunnel). The cookie set at login still covers same-
 * origin refreshes; the stored token covers everything else. */
let token = localStorage.getItem("ab_token") || null;
let ws = null;
let wsState = "closed";
let manualControl = false;
let viewport = { w: 1366, h: 900 };
let reconnectTimer = null;
let lastMove = 0;

/* ── login ─────────────────────────────────────────────────────────── */

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(API + path, {
    credentials: "include",
    ...opts,
    headers,
  });
  return resp;
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  const resp = await api("/api/login", {
    method: "POST",
    body: JSON.stringify({ code: $("login-code").value }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    $("login-error").textContent = data.error || "sign-in failed";
    return;
  }
  const data = await resp.json();
  token = data.token;
  localStorage.setItem("ab_token", token);
  showMain();
});

async function boot() {
  const resp = await api("/api/me");
  if (resp.ok) {
    showMain();
  } else {
    token = null;
    localStorage.removeItem("ab_token");
  }
}

function showMain() {
  $("login-view").classList.add("hidden");
  $("main-view").classList.remove("hidden");
  connect();
}

$("btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  localStorage.removeItem("ab_token");
  location.reload();
});

/* ── websocket ─────────────────────────────────────────────────────── */

function wsBase() {
  if (API) {
    const u = new URL(API);
    return `${u.protocol === "https:" ? "wss" : "ws"}://${u.host}`;
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}`;
}

function connect() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  ws = new WebSocket(`${wsBase()}/ws/screen${q}`);
  wsState = "connecting";
  setConn("connecting…");

  ws.onopen = () => {
    wsState = "open";
    setConn("live");
    $("conn").classList.add("live");
  };
  ws.onclose = () => {
    wsState = "closed";
    setConn("offline");
    $("conn").classList.remove("live");
    scheduleReconnect();
  };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handle(msg);
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 2000);
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function setConn(text, manual) {
  const pill = $("conn");
  pill.textContent = text;
  pill.classList.toggle("manual", !!manual);
}

/* ── inbound messages ──────────────────────────────────────────────── */

function handle(msg) {
  switch (msg.type) {
    case "hello":
      viewport = msg.viewport || viewport;
      $("client-name").textContent = msg.client?.display_name || "";
      applyBrowser(msg.browser);
      applyMonitor(msg.monitor);
      break;
    case "frame":
      $("screen-overlay").style.display = "none";
      $("screen").src = "data:image/jpeg;base64," + msg.data;
      break;
    case "status":
      applyBrowser(msg);
      break;
    case "monitor":
      applyMonitorState(msg.state);
      break;
    case "alert":
      prependFeed($("alerts"), feedLine(msg.alert, `${msg.alert.date} ${msg.alert.time || ""} — ${msg.alert.category}`));
      break;
    case "booking":
      applyBooking(msg.booking);
      prependFeed($("bookings"), feedLine(msg.booking, `${msg.booking.date || ""} ${msg.booking.time || ""} — ${msg.booking.status}`));
      break;
    case "nav_blocked":
      showBanner(`Navigation blocked: ${msg.url} is outside your portal.`, 6000);
      break;
    case "error":
      showBanner(msg.message || "input rejected", 4000);
      break;
  }
}

function applyBrowser(b) {
  if (!b) return;
  const was = manualControl;
  manualControl = b.control === "manual";
  $("screen-wrap").classList.toggle("manual", manualControl);
  $("btn-control").textContent = manualControl ? "Release control" : "Take control";
  $("btn-control").classList.toggle("active", manualControl);
  if (manualControl !== was) setConn(wsState === "open" ? "live" : "offline", manualControl);
  if (b.url) document.title = `AB — ${b.title || b.url}`;
}

function applyMonitor(snap) {
  if (!snap) return;
  if (snap.monitor) applyMonitorState(snap.monitor);
  if (snap.booking && Object.keys(snap.booking).length) applyBooking(snap.booking);
  (snap.alerts || []).forEach((a) => prependFeed($("alerts"), feedLine(a, `${a.date} ${a.time || ""} — ${a.category}`)));
  (snap.bookings || []).forEach((b) => prependFeed($("bookings"), feedLine(b, `${b.date || ""} ${b.time || ""} — ${b.status}`)));
}

function applyMonitorState(state) {
  if (!state) return;
  const el = $("monitor-status");
  const label = (state.status || "—").replaceAll("_", " ");
  el.innerHTML = `<span class="status-dot ${state.status}"></span>${label}` +
    (state.message ? ` <span class="muted">— ${escapeHtml(state.message)}</span>` : "");
  renderMetrics(state);
  renderSlots(state.slots || []);
  if (state.status === "needs_human") {
    showBanner("A slot was claimed for you — take control and finish the form now.", 0);
    setBookingActions(true);
    startAlarm(currentAlarmKey());
  } else if (state.status === "booked") {
    showBanner("Booking confirmed — no further action needed.", 0);
    setBookingActions(false);
    stopAlarm();
  } else if (state.status === "awaiting_login") {
    showBanner("Portal sign-in required — take control and log in.", 0);
    setBookingActions(false);
    stopAlarm();
  } else if (state.status === "manual_control") {
    showBanner("You are in control. Automation is paused.", 0);
    // keep any active booking actions visible; only sound stops being re-armed
  } else {
    hideBanner();
    setBookingActions(false);
  }
}

function renderMetrics(state) {
  const el = $("monitor-metrics");
  const parts = [];
  if (state.last_check) {
    const ago = Math.max(0, Math.round(Date.now() / 1000 - state.last_check));
    parts.push(`last check ${ago}s ago`);
  } else {
    parts.push("no successful check yet");
  }
  if (state.latency_ms != null) parts.push(`took ${state.latency_ms}ms`);
  if (state.effective_interval_s) {
    parts.push(`every ~${Math.round(state.effective_interval_s)}s`);
  }
  if (state.throttle) parts.push(`paused: ${state.throttle}`);
  el.textContent = parts.join(" · ");
}

function applyBooking(b) {
  if (!b || !b.status) return;
  latestBooking = b;
  const el = $("booking-status");
  if (b.status === "needs_human") {
    el.innerHTML = `<p class="needs-human">⚠ Slot held ${b.slot?.date || b.date || ""} ${b.slot?.time || b.time || ""} — complete the form in the browser.</p>`;
    startAlarm(currentAlarmKey(b));
  } else if (b.status === "booked") {
    el.innerHTML = `<p class="booked">✓ Booked — ref ${escapeHtml(b.confirmation_ref || "")}</p>`;
  }
}

/* ── client-side alarm ───────────────────────────────────────────────
 * The sound lives in the CLIENT's browser, not on the host. Browsers only
 * allow audio after a user gesture (Enable sound) and can throttle timers
 * in background tabs — the persistent banner/booking card is the reliable
 * channel; sound is a bonus. Acknowledge silences the current alert and is
 * remembered across reconnects (localStorage, keyed by the slot).
 */
let audioCtx = null;
let alarmTimer = null;
let alarmKey = null;
let latestBooking = null;

function currentAlarmKey(b) {
  const src = b || latestBooking || {};
  return `${src.profile || ""}|${src.date || ""}|${src.time || ""}`;
}

function ackedKey() {
  return localStorage.getItem("ab_acked_alert") || "";
}

function setBookingActions(show) {
  $("booking-actions").classList.toggle("hidden", !show);
}

function startAlarm(key) {
  alarmKey = key;
  if (!key || key === ackedKey()) return;
  if (!audioCtx) return;  // sound not enabled — banner still shows
  if (alarmTimer) return;
  alarmTimer = setInterval(beep, 1200);
  beep();
}

function stopAlarm() {
  if (alarmTimer) clearInterval(alarmTimer);
  alarmTimer = null;
}

function beep() {
  if (!audioCtx) return;
  try {
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.type = "sine"; o.frequency.value = 880;
    g.gain.value = 0.12;
    o.start(); o.stop(audioCtx.currentTime + 0.4);
  } catch (_) { /* audio suspended — banner still shows */ }
}

$("btn-sound").addEventListener("click", async () => {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.resume();
  $("btn-sound").classList.add("hidden");
  $("btn-test-sound").classList.remove("hidden");
  // a needs_human may already be on screen — arm it now
  if (alarmKey) { stopAlarm(); alarmTimer = null; startAlarm(alarmKey); }
});

$("btn-test-sound").addEventListener("click", beep);

$("btn-ack").addEventListener("click", () => {
  if (alarmKey) localStorage.setItem("ab_acked_alert", alarmKey);
  stopAlarm();
});

$("btn-finished").addEventListener("click", async () => {
  const resp = await api("/api/booking/complete", { method: "POST" });
  const data = await resp.json().catch(() => ({}));
  if (data.booked) {
    stopAlarm();
    showBanner(`Booking confirmed (${data.confirmation_ref || "ok"}).`, 0);
    setBookingActions(false);
  } else {
    showBanner(data.error || "Confirmation not visible yet — finish the form first.", 6000);
  }
});

function renderSlots(slots) {
  $("slot-count").textContent = slots.length ? `(${slots.length})` : "";
  const ul = $("slots");
  ul.innerHTML = "";
  slots.forEach((s) => {
    const li = document.createElement("li");
    li.innerHTML = `${escapeHtml(s.date)} ${escapeHtml(s.time || "")}` +
      `<span class="when">${escapeHtml(s.category)} @ ${escapeHtml(s.location)}${s.active === 0 ? " — gone" : ""}</span>`;
    ul.appendChild(li);
  });
}

function feedLine(item, text) {
  const li = document.createElement("li");
  const when = item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : (item.sent_at || item.started_at || "");
  li.innerHTML = `${escapeHtml(text)}<span class="when">${escapeHtml(when)} ${escapeHtml(item.status || "")}</span>`;
  return li;
}

function prependFeed(ul, li) {
  ul.insertBefore(li, ul.firstChild);
  while (ul.children.length > 60) ul.removeChild(ul.lastChild);
}

let bannerTimer = null;
function showBanner(text, ms) {
  const el = $("banner");
  el.textContent = text;
  el.classList.remove("hidden");
  if (bannerTimer) clearTimeout(bannerTimer);
  if (ms) bannerTimer = setTimeout(hideBanner, ms);
}
function hideBanner() {
  $("banner").classList.add("hidden");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── control ───────────────────────────────────────────────────────── */

$("btn-control").addEventListener("click", () => {
  send({ type: "control", action: manualControl ? "release" : "take" });
});

$("btn-home").addEventListener("click", () => {
  send({ type: "navigate", target: "home" });
});

/* ── input forwarding ──────────────────────────────────────────────── */

const screen = $("screen");
const wrap = $("screen-wrap");

function toViewport(e) {
  const r = screen.getBoundingClientRect();
  const x = Math.round(((e.clientX - r.left) / r.width) * viewport.w);
  const y = Math.round(((e.clientY - r.top) / r.height) * viewport.h);
  return { x: Math.max(0, Math.min(viewport.w, x)), y: Math.max(0, Math.min(viewport.h, y)) };
}

const BUTTONS = { 0: "left", 1: "middle", 2: "right" };

wrap.addEventListener("contextmenu", (e) => e.preventDefault());

wrap.addEventListener("mousemove", (e) => {
  if (!manualControl) return;
  const now = performance.now();
  if (now - lastMove < 40) return;
  lastMove = now;
  send({ type: "mouse", event: "move", ...toViewport(e) });
});

wrap.addEventListener("mousedown", (e) => {
  if (!manualControl) return;
  e.preventDefault();
  send({ type: "mouse", event: "down", ...toViewport(e), button: BUTTONS[e.button] || "left" });
});

wrap.addEventListener("mouseup", (e) => {
  if (!manualControl) return;
  send({ type: "mouse", event: "up", ...toViewport(e), button: BUTTONS[e.button] || "left" });
});

wrap.addEventListener("wheel", (e) => {
  if (!manualControl) return;
  e.preventDefault();
  send({ type: "wheel", ...toViewport(e), dx: Math.round(e.deltaX), dy: Math.round(e.deltaY) });
}, { passive: false });

wrap.addEventListener("dblclick", (e) => {
  if (!manualControl) return;
  send({ type: "mouse", event: "click", ...toViewport(e), button: "left", clicks: 2 });
});

/* Keys are captured globally while the client holds control — the remote
 * page is the only place they can go. Skipped while a dashboard field has
 * focus (e.g. nothing focused during login since the main view is hidden). */
const SKIP_KEYS = new Set(["F5", "F11", "F12"]);

document.addEventListener("keydown", (e) => {
  if (!manualControl) return;
  if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
  if (SKIP_KEYS.has(e.key)) return;
  e.preventDefault();
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    send({ type: "text", text: e.key });
  } else {
    send({ type: "key", event: "press", key: e.key });
  }
});

document.addEventListener("keyup", (e) => {
  if (!manualControl) return;
  if (["Shift", "Control", "Alt", "Meta"].includes(e.key)) {
    send({ type: "key", event: "up", key: e.key });
  }
});

/* ── records refresh (slow poll; cheap) ────────────────────────────── */

async function refreshRecords() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const resp = await api("/api/records");
  if (!resp.ok) return;
  const data = await resp.json();
  renderSlots((data.slots || []).filter((s) => s.active !== 0));
  renderFeed($("alerts"), (data.alerts || []),
    (a) => `${a.date} ${a.time || ""} — ${a.category}`);
  renderFeed($("bookings"), (data.bookings || []),
    (b) => `${b.date || ""} ${b.time || ""} — ${b.status}`);
}

function renderFeed(ul, items, fmt) {
  // Live WS events prepend instantly; this paints the persisted record list
  // beneath them (deduped loosely by text to avoid flicker).
  if (!items.length) return;
  const existing = new Set([...ul.children].map((li) => li.textContent.slice(0, 40)));
  items.forEach((item) => {
    const text = fmt(item);
    if (existing.has(text.slice(0, 40))) return;
    ul.appendChild(feedLine(item, text));
  });
}

setInterval(refreshRecords, 15000);

boot();

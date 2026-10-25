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

let ws = null;
let wsState = "closed";
let manualControl = false;
let viewport = { w: 1366, h: 900 };
let reconnectTimer = null;
let lastMove = 0;

/* ── login ─────────────────────────────────────────────────────────── */

async function api(path, opts = {}) {
  const resp = await fetch(API + path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  return resp;
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  const resp = await api("/api/login", {
    method: "POST",
    body: JSON.stringify({
      username: $("login-username").value,
      password: $("login-password").value,
    }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    $("login-error").textContent = data.error || "sign-in failed";
    return;
  }
  showMain();
});

async function boot() {
  const resp = await api("/api/me");
  if (resp.ok) {
    showMain();
  }
}

function showMain() {
  $("login-view").classList.add("hidden");
  $("main-view").classList.remove("hidden");
  connect();
}

$("btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
});

/* ── websocket ─────────────────────────────────────────────────────── */

function connect() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/ws/screen`);
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
  renderSlots(state.slots || []);
  if (state.status === "needs_human") {
    showBanner("A slot was claimed for you — take control and finish the form now.", 0);
  } else if (state.status === "awaiting_login") {
    showBanner("Portal sign-in required — take control and log in.", 0);
  } else if (state.status === "manual_control") {
    showBanner("You are in control. Automation is paused.", 0);
  } else {
    hideBanner();
  }
}

function applyBooking(b) {
  if (!b || !b.status) return;
  const el = $("booking-status");
  if (b.status === "needs_human") {
    el.innerHTML = `<p class="needs-human">⚠ Slot held ${b.slot?.date || b.date || ""} ${b.slot?.time || b.time || ""} — complete the form in the browser.</p>`;
  }
}

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

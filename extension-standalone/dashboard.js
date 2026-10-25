const $ = (id) => document.getElementById(id);
const ask = (m) => new Promise((r) => chrome.runtime.sendMessage(m, r));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const REDACT = /pass|pwd|secret|token|key|email/i;

async function refresh() {
  const s = await ask({ type: "getState" });
  if (!s) return;
  $("monitoring").checked = !!s.settings.monitoring;
  $("pollMin").value = s.settings.pollMinutes || 5;
  const tabs = s.tabs || [];
  $("tab").innerHTML = tabs.map((t) =>
    `<option value="${t.id}" ${t.id === s.settings.selectedTabId ? "selected" : ""}>${esc((t.title || t.url || "").slice(0, 50))}</option>`).join("");
  $("cfg").value = JSON.stringify({ selectors: s.settings.selectors, profiles: s.settings.profiles, booking: s.settings.booking }, null, 1);

  $("profiles").innerHTML = (s.settings.profiles || []).map((p) => {
    const lp = (s.lastPoll || {})[p.name] || {};
    const cls = lp.status === "ok" ? "ok" : "err";
    return `<div class="card"><b>${esc(p.name)}</b> <span class="${cls}">${esc(lp.status || "never polled")}</span>
      ${lp.error ? `<div class="err">${esc(lp.error)}</div>` : ""}
      <div>${esc(p.category)} @ ${esc(p.location)} · slots=${lp.slots ? lp.slots.length : 0} new=${lp.fresh || 0} · last ${esc(lp.at || "—")}</div>
      ${lp.booking ? `<div class="warn">booking: ${esc(lp.booking.status)}</div>` : ""}</div>`;
  }).join("") || "<i>no profiles — add one in Settings JSON</i>";

  $("slots").innerHTML = Object.entries(s.slots || {}).filter(([, r]) => r.active).map(([k, r]) =>
    `<div>${esc(k)} <span class="ok">${r.alerted ? "notified" : "new"}</span></div>`).join("") || "<i>none</i>";

  $("bookings").innerHTML = (s.bookings || []).map((b) => {
    const open = b.finishedAt == null, unres = ["needs_human", "aborted_unknown_state"].includes(b.status);
    return `<div class="card">#${b.id} ${esc(b.status)} ${esc(b.profile)} ${esc(b.date)} ${esc(b.time || "")}${open ? " <b class='err'>IN FLIGHT</b>" : ""}${unres ? " <b class='warn'>UNRESOLVED</b>" : ""}
      ${open || unres ? `<button class="sec" onclick="resolve(${b.id})">Resolve</button>` : ""}</div>`;
  }).join("") || "<i>none</i>";

  $("alerts").innerHTML = (s.alerts || []).slice(0, 30).map((a) =>
    `<div>${new Date(a.ts).toLocaleTimeString()} ${esc(a.date)} ${esc(a.time || "")} ${esc(a.category)} @ ${esc(a.location)} <span class="${a.status === "sent" ? "ok" : "err"}">${esc(a.status)}</span></div>`).join("") || "<i>none</i>";
}

window.resolve = async (id) => { await ask({ type: "resolveBooking", id }); refresh(); };
$("save").onclick = async () => {
  const tab = $("tab").value;
  await ask({ type: "saveSettings", settings: { monitoring: $("monitoring").checked, pollMinutes: +$("pollMin").value || 5, selectedTabId: tab ? +tab : null } });
  refresh();
};
$("poll").onclick = async () => { await ask({ type: "pollNow" }); setTimeout(refresh, 1500); };
$("stopAlarm").onclick = () => ask({ type: "stopAlarm" });
$("saveCfg").onclick = async () => {
  try { await ask({ type: "saveSettings", settings: JSON.parse($("cfg").value) }); refresh(); }
  catch (e) { alert("invalid JSON: " + e.message); }
};
$("export").onclick = () => {
  const cfg = JSON.parse($("cfg").value); // selectors/profiles/booking only — no secrets exist
  dl("appointment-booker-config.json", JSON.stringify(cfg, null, 1));
};
$("import").onchange = async (e) => {
  const text = await e.target.files[0].text();
  try { $("cfg").value = JSON.stringify(JSON.parse(text), null, 1); } catch { alert("not a config file"); }
};
$("diag").onclick = async () => {
  const s = await ask({ type: "getState" });
  const redact = (o) => JSON.parse(JSON.stringify(o, (k, v) => (REDACT.test(k) && typeof v === "string") ? "***" : v));
  dl("appointment-booker-diag.json", JSON.stringify(redact(s), null, 1));
};
function dl(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  a.download = name; a.click(); URL.revokeObjectURL(a.href);
}
refresh(); setInterval(refresh, 5000);

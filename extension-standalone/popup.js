const dot = document.getElementById("dot");
const statusEl = document.getElementById("status");
chrome.runtime.sendMessage({ type: "getState" }, (s) => {
  if (!s || s.ok === false) { statusEl.textContent = "Extension idle."; return; }
  const on = s.settings && s.settings.monitoring;
  dot.classList.toggle("on", !!on);
  const anyErr = Object.values(s.lastPoll || {}).find((p) => p.status !== "ok");
  statusEl.textContent = on
    ? (anyErr ? `Monitoring — last: ${anyErr.status}` : "Monitoring active.")
    : "Monitoring off — open the dashboard to start.";
});
document.getElementById("open").onclick = () =>
  chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });

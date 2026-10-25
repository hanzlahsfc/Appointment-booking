/** Popup: shows bridge status and lets the operator pick the tab to drive. */

const els = {
  dot: document.getElementById("dot"),
  port: document.getElementById("port"),
  tab: document.getElementById("tab"),
  connect: document.getElementById("connect"),
  disconnect: document.getElementById("disconnect"),
  error: document.getElementById("error"),
};

function ask(type, payload = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, ...payload }, (reply) => resolve(reply));
  });
}

function render(bridgeStatus) {
  els.dot.classList.toggle("on", Boolean(bridgeStatus && bridgeStatus.connected));
  if (bridgeStatus) {
    els.port.value = bridgeStatus.port;
    els.error.textContent = bridgeStatus.lastError
      ? `Last error: ${bridgeStatus.lastError}`
      : bridgeStatus.connected
        ? "Connected to the local agent."
        : "Not connected — start the crawler, then press Connect.";
  }
}

async function loadTabs(selectedId) {
  const tabs = await chrome.tabs.query({});
  els.tab.innerHTML = "";
  for (const tab of tabs) {
    const option = document.createElement("option");
    option.value = String(tab.id);
    option.textContent = `${tab.title ? tab.title.slice(0, 42) : tab.url} ${tab.active ? "(active)" : ""}`;
    if (selectedId != null && String(tab.id) === String(selectedId)) option.selected = true;
    els.tab.appendChild(option);
  }
}

async function refresh() {
  const status = await ask("status");
  render(status);
  await loadTabs(status && status.tabId);
}

els.connect.addEventListener("click", async () => {
  await ask("setPort", { port: Number(els.port.value) });
  render(await ask("connect"));
});

els.disconnect.addEventListener("click", async () => {
  render(await ask("disconnect"));
});

els.tab.addEventListener("change", async () => {
  render(await ask("selectTab", { tabId: Number(els.tab.value) }));
});

refresh();

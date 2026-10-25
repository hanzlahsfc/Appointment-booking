/** Popup: shows bridge status and lets the operator pick the tab to drive. */

const els = {
  dot: document.getElementById("dot"),
  port: document.getElementById("port"),
  token: document.getElementById("token"),
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
    if (bridgeStatus.lastError) {
      els.error.textContent = `Last error: ${bridgeStatus.lastError}`;
    } else if (bridgeStatus.connected) {
      els.error.textContent = "Connected to the local agent.";
    } else if (bridgeStatus.desiredConnected) {
      els.error.textContent = "Trying to reach the local agent — is the crawler running?";
    } else {
      els.error.textContent = "Disconnected. Press Connect when the crawler is running.";
    }
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
  if (els.token.value) await ask("setToken", { token: els.token.value });
  render(await ask("connect"));
});

els.disconnect.addEventListener("click", async () => {
  render(await ask("disconnect"));
});

els.tab.addEventListener("change", async () => {
  render(await ask("selectTab", { tabId: Number(els.tab.value) }));
});

refresh();

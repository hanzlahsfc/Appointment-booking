/**
 * Appointment Booker Bridge — service worker.
 *
 * Connects this browser to the local Appointment Booker agent over a WebSocket
 * and executes Chrome DevTools Protocol commands on the tab the operator picks.
 *
 * Why an extension at all: Chrome 136+ refuses --remote-debugging-port on the
 * default profile, so nothing can attach to the browser you actually use.
 * chrome.debugger, granted by the person who installs this, can. That means the
 * agent works with the real, already-signed-in session instead of a fresh
 * profile that has to log in again past the reCAPTCHA.
 *
 * Design notes:
 *  - No native messaging host and no registry keys: the agent already serves
 *    HTTP/WebSocket on loopback, so the extension dials out to it.
 *  - An open WebSocket resets the service-worker idle timer (Chrome 116+); a 30s
 *    alarm re-arms it, but only when the operator wants a connection.
 *  - Disconnect is sticky: `desiredConnected` is persisted, so closing the
 *    socket on purpose is not undone three seconds later by the reconnect path.
 *  - Nothing is automated until the operator chooses a tab in the popup, and any
 *    change of tab is pushed to the agent so it never commands a stale tab.
 */

const DEFAULT_PORT = 8080;
const RECONNECT_MS = 3000;
const MAX_NETWORK_ENTRIES = 300;
const PROTOCOL_VERSION = 1;

let socket = null;
let settings = { port: DEFAULT_PORT, tabId: null, desiredConnected: false, token: "" };
let connecting = false;
let lastError = null;

/** Recent network activity per tab, for the recon pass. */
const networkLog = new Map();
/** Request metadata keyed by "<tabId>:<requestId>" — tab-scoped, never global. */
const pendingRequests = new Map();

// ── settings ────────────────────────────────────────────────────────────────

async function loadSettings() {
  const stored = await chrome.storage.local.get({
    port: DEFAULT_PORT,
    tabId: null,
    desiredConnected: false,
    token: "",
  });
  settings = {
    port: Number(stored.port) || DEFAULT_PORT,
    tabId: stored.tabId === undefined ? null : stored.tabId,
    desiredConnected: Boolean(stored.desiredConnected),
    token: stored.token || "",
  };
  return settings;
}

async function setDesired(connected) {
  settings.desiredConnected = connected;
  await chrome.storage.local.set({ desiredConnected: connected });
}

// ── socket ──────────────────────────────────────────────────────────────────

function isOpen() {
  return Boolean(socket && socket.readyState === WebSocket.OPEN);
}

function status() {
  return {
    connected: isOpen(),
    desiredConnected: settings.desiredConnected,
    port: settings.port,
    tabId: settings.tabId,
    tokenSet: Boolean(settings.token),
    lastError,
    protocol: PROTOCOL_VERSION,
  };
}

function send(payload) {
  if (!isOpen()) return false;
  try {
    socket.send(JSON.stringify(payload));
    return true;
  } catch (err) {
    lastError = String(err);
    return false;
  }
}

async function connect() {
  if (connecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  await loadSettings();
  if (!settings.desiredConnected) return; // explicit Connect only

  connecting = true;
  try {
    const token = settings.token ? `?token=${encodeURIComponent(settings.token)}` : "";
    socket = new WebSocket(`ws://127.0.0.1:${settings.port}/bridge${token}`);
  } catch (err) {
    lastError = String(err);
    connecting = false;
    return;
  }

  socket.onopen = () => {
    connecting = false;
    lastError = null;
    send({
      type: "hello",
      protocol: PROTOCOL_VERSION,
      version: chrome.runtime.getManifest().version,
      tabId: settings.tabId,
    });
  };

  socket.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (!message || typeof message !== "object") return;
    if (message.type === "ping") {
      send({ type: "pong" });
      return;
    }
    if (typeof message.id === "string") handleRequest(message);
  };

  socket.onerror = () => {
    connecting = false;
  };

  socket.onclose = () => {
    connecting = false;
    socket = null;
    reconnectLater();
  };
}

function reconnectLater() {
  if (!settings.desiredConnected) return;
  setTimeout(() => {
    if (settings.desiredConnected) connect().catch(() => {});
  }, RECONNECT_MS);
}

async function disconnect() {
  await setDesired(false);
  if (socket) {
    try {
      socket.close();
    } catch (err) {
      // already closing
    }
  }
  socket = null;
  return status();
}

// ── request handling ────────────────────────────────────────────────────────

/** Accept both wire spellings so a protocol skew cannot silently break commands. */
function tabIdOf(message) {
  return message.tabId !== undefined ? message.tabId : message.tab_id;
}

async function handleRequest(message) {
  const { id, op } = message;
  try {
    let result;
    switch (op) {
      case "status":
        result = status();
        break;
      case "tabs":
        result = await listTabs();
        break;
      case "command": {
        const method = message.method;
        if (typeof method !== "string" || !method) {
          throw new Error("command.method must be a non-empty string");
        }
        const params = message.params;
        if (params !== undefined && (params === null || typeof params !== "object")) {
          throw new Error("command.params must be an object");
        }
        result = await runCommand(tabIdOf(message), method, params || {});
        break;
      }
      case "network":
        result = readNetwork(tabIdOf(message), message);
        break;
      case "networkClear":
        clearNetwork(tabIdOf(message));
        result = { cleared: true };
        break;
      default:
        throw new Error(`unknown op: ${String(op)}`);
    }
    send({ id, ok: true, result });
  } catch (err) {
    send({ id, ok: false, error: String((err && err.message) || err) });
  }
}

async function listTabs() {
  const tabs = await chrome.tabs.query({});
  return tabs.map((tab) => ({
    tabId: tab.id,
    url: tab.url,
    title: tab.title,
    active: Boolean(tab.active),
    windowId: tab.windowId,
  }));
}

// ── CDP ─────────────────────────────────────────────────────────────────────

async function attach(tabId) {
  if (!tabId) throw new Error("no tab selected — pick one in the extension popup");
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (err) {
    // "Another debugger is already attached" is fine — we can still command it.
    const text = String((err && err.message) || err);
    if (!/already attached/i.test(text)) throw err;
  }
  try {
    await chrome.debugger.sendCommand(target, "Network.enable", {});
  } catch (err) {
    // network capture is best-effort; never block a command on it
  }
  return target;
}

async function runCommand(tabId, method, params) {
  const target = await attach(tabId);
  return chrome.debugger.sendCommand(target, method, params);
}

// ── network capture (recon needs request AND response bodies) ───────────────

function requestKey(tabId, requestId) {
  return `${tabId}:${requestId}`;
}

function logFor(tabId) {
  if (!networkLog.has(tabId)) networkLog.set(tabId, []);
  return networkLog.get(tabId);
}

function purgePending(tabId) {
  const prefix = `${tabId}:`;
  for (const key of [...pendingRequests.keys()]) {
    if (key.startsWith(prefix)) pendingRequests.delete(key);
  }
}

function truncate(value, limit = 200000) {
  if (typeof value !== "string") return value;
  return value.length > limit ? `${value.slice(0, limit)}…[truncated]` : value;
}

chrome.debugger.onEvent.addListener(async (source, method, params) => {
  const tabId = source && source.tabId;
  if (!tabId) return;
  const key = requestKey(tabId, params.requestId);

  if (method === "Network.requestWillBeSent") {
    const entry = {
      requestId: params.requestId,
      method: params.request.method,
      url: params.request.url,
      type: params.type,
      headers: params.request.headers,
      postData: params.request.postData ? truncate(params.request.postData) : undefined,
      startedAt: Date.now(),
    };
    pendingRequests.set(key, entry);
    logFor(tabId).push(entry);
    return;
  }

  if (method === "Network.responseReceived") {
    const entry = pendingRequests.get(key);
    if (entry) {
      entry.status = params.response.status;
      entry.mimeType = params.response.mimeType;
      entry.responseHeaders = params.response.headers;
    }
    return;
  }

  if (method === "Network.loadingFailed") {
    // A failed request never reaches loadingFinished: finalise and drop it here
    // so the pending map cannot grow without bound.
    const entry = pendingRequests.get(key);
    if (entry) {
      entry.failed = true;
      entry.errorText = params.errorText || "request failed";
    }
    pendingRequests.delete(key);
    return;
  }

  if (method === "Network.loadingFinished") {
    const entry = pendingRequests.get(key);
    if (!entry) return;
    const interesting =
      /json|text|xml|javascript/i.test(entry.mimeType || "") ||
      (entry.type || "").toLowerCase() === "xhr" ||
      (entry.type || "").toLowerCase() === "fetch";
    if (interesting) {
      try {
        const body = await chrome.debugger.sendCommand(
          { tabId },
          "Network.getResponseBody",
          { requestId: params.requestId }
        );
        entry.body = truncate(body && body.body);
        entry.base64Encoded = Boolean(body && body.base64Encoded);
      } catch (err) {
        // body may be evicted; metadata is still useful
      }
    }
    pendingRequests.delete(key);
    const log = logFor(tabId);
    while (log.length > MAX_NETWORK_ENTRIES) log.shift();
  }
});

// Detaching (tab closed, debugger stolen) leaves nothing joinable behind.
chrome.debugger.onDetach.addListener((source) => {
  if (source && source.tabId) purgePending(source.tabId);
});

// Closing a tab must drop both its trace and its pending requests.
chrome.tabs.onRemoved.addListener(async (tabId) => {
  networkLog.delete(tabId);
  purgePending(tabId);
  if (settings.tabId === tabId) {
    settings.tabId = null;
    await chrome.storage.local.set({ tabId: null });
    send({ type: "tabChanged", tabId: null });
  }
});

function readNetwork(tabId, options = {}) {
  const limit = Number(options.limit) || 50;
  const urlIncludes = options.urlIncludes ? String(options.urlIncludes) : null;
  let entries = logFor(tabId);

  if (options.onlyApi) {
    entries = entries.filter((e) => ["XHR", "Fetch"].includes(e.type));
  }
  if (options.type) {
    entries = entries.filter((e) => e.type === options.type);
  }
  if (urlIncludes) {
    entries = entries.filter((e) => (e.url || "").includes(urlIncludes));
  }
  if (options.includeBodies === false) {
    entries = entries.map(({ body, ...rest }) => rest);
  }
  return { entries: entries.slice(-limit), total: entries.length };
}

function clearNetwork(tabId) {
  if (tabId) {
    networkLog.delete(tabId);
    purgePending(tabId);
    return;
  }
  networkLog.clear();
  pendingRequests.clear();
}

// ── lifecycle ───────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.5 });
  connect().catch(() => {}); // no-op unless desiredConnected was already true
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.5 });
  connect().catch(() => {});
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "bridge-keepalive") return;
  if (isOpen()) {
    send({ type: "ping" });
  } else if (settings.desiredConnected) {
    connect().catch(() => {});
  }
});

// The popup drives connect/disconnect and tab selection.
chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  (async () => {
    if (!message || typeof message !== "object") {
      respond(status());
      return;
    }
    if (message.type === "connect") {
      await setDesired(true);
      await connect();
      respond(status());
    } else if (message.type === "disconnect") {
      respond(await disconnect());
    } else if (message.type === "selectTab") {
      const tabId = Number(message.tabId);
      settings.tabId = Number.isFinite(tabId) ? tabId : null;
      await chrome.storage.local.set({ tabId: settings.tabId });
      // Tell the agent immediately, so it stops commanding the previous tab.
      send({ type: "tabChanged", tabId: settings.tabId });
      respond(status());
    } else if (message.type === "setPort") {
      settings.port = Number(message.port) || DEFAULT_PORT;
      await chrome.storage.local.set({ port: settings.port });
      if (socket) socket.close();
      socket = null;
      await connect();
      respond(status());
    } else if (message.type === "setToken") {
      settings.token = String(message.token || "");
      await chrome.storage.local.set({ token: settings.token });
      respond(status());
    } else {
      respond(status());
    }
  })();
  return true; // keep the message channel open for the async reply
});

connect().catch(() => {}); // resumes only if the operator left it connected

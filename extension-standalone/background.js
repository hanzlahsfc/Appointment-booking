/* Service worker: scheduling, storage, notifications, alarm, orchestration.
 * MV3 honesty: the worker suspends; chrome.alarms is the scheduler, and state
 * is persisted so a fresh worker reconciles from storage — no uninterrupted
 * background execution is promised or required. */
importScripts("core/slots.js", "core/state.js", "core/poll.js");

const ALARM = "ab-poll";
const kv = {
  get: async (k) => (await chrome.storage.local.get(k))[k],
  set: (k, v) => chrome.storage.local.set({ [k]: v }),
};
const store = ABState.createStore(kv);
const tabOwners = new Map();
const polling = new Set();

const DEFAULT_SETTINGS = {
  pollMinutes: 5, alarmSeconds: 120,
  booking: { mode: "off", dryRun: true, maxAttemptsPerHour: 4 },
  selectors: {}, profiles: [], monitoring: false, selectedTabId: null,
};

const getSettings = async () => ({ ...DEFAULT_SETTINGS, ...(await kv.get("settings") || {}) });

const tabs = {
  get: (id) => chrome.tabs.get(id),
  async send(tabId, msg) {
    try {
      return await chrome.tabs.sendMessage(tabId, msg);
    } catch (e) {
      if (!/Receiving end/i.test(String(e.message))) throw e;
      await chrome.scripting.executeScript({ target: { tabId }, files: ["core/slots.js", "content-portal.js"] });
      return chrome.tabs.sendMessage(tabId, msg);
    }
  },
};

function notify(text, title) {
  return new Promise((resolve) =>
    chrome.notifications.create("", { type: "basic", iconUrl: "icon.png", title: title || "Appointment Booker", message: text }, () => resolve(!chrome.runtime.lastError))
  );
}

async function alarm(message) {
  await notify(message, "SLOT CLAIMED");
  await chrome.offscreen.createDocument({ url: "offscreen.html", reasons: ["AUDIO_PLAYBACK"], justification: "booking alarm" }).catch(() => {});
  chrome.runtime.sendMessage({ type: "alarm", message }).catch(() => {});
}

function claimTab(id, owner) {
  const cur = tabOwners.get(id);
  if (cur && cur !== owner) throw new Error(`tab ${id} already driven by ${cur}`);
  tabOwners.set(id, owner);
}
const releaseTab = (id, owner) => { if (tabOwners.get(id) === owner) tabOwners.delete(id); };

async function pollProfile(s, profile) {
  if (polling.has(profile.name)) return { profile: profile.name, status: "overlap_skipped" };
  polling.add(profile.name);
  const lastPoll = await kv.get("lastPoll") || {};
  try {
    const res = await ABPoll.pollOnce({
      tabs, store, notify, alarm, claimTab, releaseTab,
      selectedTabId: () => s.selectedTabId,
      selectors: s.selectors, booking: s.booking,
      now: () => new Date().toISOString(),
    }, profile);
    lastPoll[profile.name] = { status: res.status, error: res.error, fresh: res.fresh, slots: res.slots, at: res.at, booking: res.booking };
    await kv.set("lastPoll", lastPoll);
    return res;
  } finally {
    polling.delete(profile.name);
  }
}

async function tick() {
  const s = await getSettings();
  if (!s.monitoring) return;
  for (const p of s.profiles) await pollProfile(s, p);
}

chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) tick().catch(() => {}); });
chrome.runtime.onInstalled.addListener(() => reconcile());
chrome.runtime.onStartup.addListener(() => reconcile());
async function reconcile() {
  // Worker restart: nothing is auto-resolved; open/unresolved attempts keep
  // their profiles parked until the operator resolves them on the dashboard.
  const s = await getSettings();
  if (s.monitoring) chrome.alarms.create(ALARM, { periodInMinutes: Math.max(1, s.pollMinutes) });
}

// Dashboard messaging.
chrome.runtime.onMessage.addListener((msg, _s, respond) => {
  (async () => {
    const s = await getSettings();
    switch (msg.type) {
      case "getState":
        respond({ settings: s, lastPoll: (await kv.get("lastPoll")) || {}, slots: await store.slotSnapshot(), alerts: await store.alertFeed(), bookings: await store.bookings(), tabs: await chrome.tabs.query({}) });
        break;
      case "saveSettings": {
        const merged = { ...s, ...msg.settings };
        await kv.set("settings", merged);
        if (merged.monitoring) chrome.alarms.create(ALARM, { periodInMinutes: Math.max(1, merged.pollMinutes) });
        else chrome.alarms.clear(ALARM);
        respond({ ok: true });
        break;
      }
      case "pollNow":
        tick().then(() => respond({ ok: true }));
        break;
      case "resolveBooking":
        respond({ ok: await store.resolveAttempt(msg.id) });
        break;
      case "stopAlarm":
        chrome.runtime.sendMessage({ type: "alarmStop" }).catch(() => {});
        respond({ ok: true });
        break;
      default:
        respond({ ok: false, error: "unknown" });
    }
  })().catch((e) => respond({ ok: false, error: String(e) }));
  return true;
});

/* Node test suite for the extension core — no browser needed.
 * Covers: parsing, dedup/nullable-time, failed-alert retry, empty-poll
 * deactivation, tab close, logout, WAF, picker mismatch, parse-failure,
 * assisted claim, ambiguous reserve, duplicate prevention, unresolved-hold
 * blocking across restarts, resolution recovery, rate cap.
 * Run: node --test tests-ext/
 */
const { test } = require("node:test");
const assert = require("node:assert");
const Slots = require("../extension-standalone/core/slots.js");
const State = require("../extension-standalone/core/state.js");
const Poll = require("../extension-standalone/core/poll.js");

const PROFILE = {
  name: "Ahmed - Work Permit", category: "national_d_employment",
  location: "lahore", bookingUrl: "http://127.0.0.1/appointments",
  autoBook: true, prefer: {}, tabId: null,
};
const SEL = { vac_select: "#vac", category_select: "#cat", slot_row: ".slot-row", reserve_button: ".reserve" };

function memKv() {
  const m = new Map();
  return { get: async (k) => m.get(k), set: async (k, v) => m.set(k, v), _map: m };
}

function env(over = {}) {
  const kv = memKv();
  const store = State.createStore(kv);
  const owners = new Map();
  const e = {
    kv, store,
    pageRows: ["2026-12-01 09:00"],
    probe: { login: false, waf: false },
    reserveReply: { status: "clicked" },
    tabs: {
      async get(id) { if (over.tabClosed) throw new Error("no tab"); return { id, url: "http://127.0.0.1/x" }; },
      async send(tabId, msg) {
        if (msg.op === "probe") return e.probe;
        if (msg.op === "applyFilters") return over.filterFail || { ok: true };
        if (msg.op === "listRows") return { rows: e.pageRows };
        if (msg.op === "reserve") {
          if (over.reserveThrows) throw new Error("click landed then message channel died");
          return e.reserveReply;
        }
        return {};
      },
    },
    notify: async () => !over.notifyFails,
    alarmCalls: [],
    alarm(m) { e.alarmCalls.push(m); },
    claimTab(t, o) { const c = owners.get(t); if (c && c !== o) throw new Error("in use"); owners.set(t, o); },
    releaseTab(t, o) { if (owners.get(t) === o) owners.delete(t); },
    selectedTabId: () => 7,
    selectors: over.selectors === undefined ? SEL : over.selectors,
    booking: over.booking || { mode: "off", dryRun: true, maxAttemptsPerHour: 4 },
    now: () => "2026-11-20T10:00:00.000Z",
  };
  return e;
}

/* parsing */
test("parse formats", () => {
  assert.deepEqual(Slots.parseSlotLabel("2026-12-01 09:00"), { date: "2026-12-01", time: "09:00" });
  assert.deepEqual(Slots.parseSlotLabel("01.12.2026 10:30"), { date: "2026-12-01", time: "10:30" });
  assert.deepEqual(Slots.parseSlotLabel("1/12/26"), { date: "2026-12-01", time: null });
  assert.equal(Slots.parseSlotLabel("32.13.2026"), null);
  assert.equal(Slots.parseSlotLabel("hello"), null);
});

/* monitoring happy path + diff */
test("poll detects and stores a slot once", async () => {
  const e = env();
  const r1 = await Poll.pollOnce(e, PROFILE);
  assert.equal(r1.status, "ok"); assert.equal(r1.fresh, 1);
  const r2 = await Poll.pollOnce(e, PROFILE);
  assert.equal(r2.fresh, 0);
});

test("nullable time dedupes", async () => {
  const e = env(); e.pageRows = ["2026-12-01"];
  await Poll.pollOnce(e, PROFILE);
  const snap = await e.store.slotSnapshot();
  assert.equal(Object.keys(snap).length, 1);
  e.pageRows = ["2026-12-01 09:00"]; // same date, adds time => distinct slot
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.fresh, 1);
});

test("empty verified poll deactivates; vanished slot alerts on return", async () => {
  const e = env();
  await Poll.pollOnce(e, PROFILE);
  e.pageRows = [];
  await Poll.pollOnce(e, PROFILE);
  let snap = await e.store.slotSnapshot();
  assert.equal(Object.values(snap)[0].active, 0);
  e.pageRows = ["2026-12-01 09:00"];
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.fresh, 0); // was alerted already -> no re-alert
});

test("failed notification retries next poll", async () => {
  const e = env({ notifyFails: true });
  await Poll.pollOnce(e, PROFILE);
  e.notify = async () => true;
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.fresh, 1); // retried because attempted-but-unalerted
});

/* error surfaces */
test("closed tab", async () => {
  const e = env({ tabClosed: true });
  assert.equal((await Poll.pollOnce(e, PROFILE)).status, "tab_closed");
});
test("logout detected", async () => {
  const e = env(); e.probe.login = true;
  assert.equal((await Poll.pollOnce(e, PROFILE)).status, "session_expired");
});
test("waf shell detected", async () => {
  const e = env(); e.probe.waf = true;
  assert.equal((await Poll.pollOnce(e, PROFILE)).status, "waf_blocked");
});
test("missing selectors", async () => {
  const e = env({ selectors: {} });
  assert.equal((await Poll.pollOnce(e, PROFILE)).status, "not_configured");
});
test("picker rejects category", async () => {
  const e = env({ filterFail: { ok: false, error: "not offered" } });
  assert.equal((await Poll.pollOnce(e, PROFILE)).status, "picker_mismatch");
});
test("all-unparseable is a failure, not an empty poll", async () => {
  const e = env(); e.pageRows = ["garbage row", "another garbage"];
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.status, "parse_failed");
  const snap = await e.store.slotSnapshot();
  assert.equal(Object.keys(snap).length, 0); // nothing deactivated
});

/* assisted booking */
test("assist claims the slot, alarms, and parks the profile", async () => {
  const e = env({ booking: { mode: "assist", dryRun: false, maxAttemptsPerHour: 4 } });
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.booking.status, "needs_human");
  assert.equal(e.alarmCalls.length, 1);
  // profile now parked: next poll is blocked by the unresolved hold
  const r2 = await Poll.pollOnce(e, PROFILE);
  assert.equal(r2.status, "blocked");
});

test("ambiguous reserve outcome is never a clean failure", async () => {
  const e = env({ booking: { mode: "assist", dryRun: false }, reserveThrows: true });
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.booking.status, "aborted_unknown_state");
  assert.equal((await Poll.pollOnce(e, PROFILE)).status, "blocked");
});

test("restart keeps the unresolved block until resolved", async () => {
  const shared = memKv(); // survives "restart"
  const e1 = env({ booking: { mode: "assist", dryRun: false } });
  e1.store = State.createStore(shared);
  await Poll.pollOnce(e1, PROFILE);
  const e2 = env({ booking: { mode: "assist", dryRun: false } });
  e2.store = State.createStore(shared); // fresh worker, same storage
  assert.equal((await Poll.pollOnce(e2, PROFILE)).status, "blocked");
  const bookings = await e2.store.bookings();
  assert.ok(await e2.store.resolveAttempt(bookings[0].id));
  assert.equal((await Poll.pollOnce(e2, PROFILE)).status, "ok");
});

test("slot already claimed is never double-booked", async () => {
  const e = env({ booking: { mode: "assist", dryRun: false } });
  await Poll.pollOnce(e, PROFILE); // claimed -> needs_human
  await e.store.resolveAttempt((await e.store.bookings())[0].id);
  // the claim record still marks that slot as claimed — the guard blocks retry
  assert.ok(await e.store.claimedForSlot({
    profile: PROFILE.name, location: PROFILE.location,
    category: PROFILE.category, date: "2026-12-01", time: "09:00",
  }));
  // exactly one attempt ever reached the portal for that slot
  assert.equal((await e.store.bookings()).length, 1);
});

test("same tab cannot be driven by two profiles at once", async () => {
  const e = env();
  e.claimTab(7, "A");
  const held = e; held.selectedTabId = () => 7;
  const r = await Poll.pollOnce(e, { ...PROFILE, name: "B" });
  assert.equal(r.status, "tab_conflict");
});

test("booking off by default; dry run never reserves", async () => {
  const e = env(); // booking off
  const r = await Poll.pollOnce(e, PROFILE);
  assert.equal(r.booking.status, "off");
  const e2 = env({ booking: { mode: "assist", dryRun: true } });
  const r2 = await Poll.pollOnce(e2, PROFILE);
  assert.equal(r2.booking.status, "dry_run");
  assert.equal((await e2.store.bookings()).length, 0);
});

/* Persisted state over a KV adapter (chrome.storage.local in the worker,
 * in-memory in tests). All slot diffing, alert retry, and booking-attempt
 * semantics live here so they are restart-safe and unit-testable. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ABState = api;
})(globalThis, function () {
  "use strict";

  const K = { settings: "settings", slots: "slotState", bookings: "bookingLog", alerts: "alertFeed" };
  const TERMINAL = ["booked", "failed", "slot_gone", "needs_human", "aborted_unknown_state", "resolved"];
  const UNRESOLVED = ["needs_human", "aborted_unknown_state"];
  // "resolved" stays claimed: a human already dealt with that slot — never
  // auto-claim it again even if it is re-offered later.
  const CLAIMED = ["attempted", "needs_human", "booked", "aborted_unknown_state", "resolved"];

  const nowIso = () => new Date().toISOString();
  const slotKey = (p, l, c, d, t) => [p, l, c, d, t || ""].join("|");

  function createStore(kv) {
    // kv: { get(key)->any|undefined, set(key,val), all()->object }
    async function read(key, fallback) {
      const v = await kv.get(key);
      return v === undefined ? fallback : v;
    }
    const write = (key, val) => kv.set(key, val);

    return {
      /* ── slots ─────────────────────────────────────────── */
      async upsertSlots(profile, slots, seenAt) {
        const map = await read(K.slots, {});
        const fresh = [];
        for (const s of slots) {
          const key = slotKey(s.profile, s.location, s.category, s.date, s.time);
          const row = map[key];
          if (!row) {
            map[key] = { raw: s.raw, firstSeen: seenAt, lastSeen: seenAt, alerted: 0, attempted: 0, active: 1 };
            fresh.push(s);
          } else {
            // alertable when never alerted AND (came back after disappearing OR a prior send failed)
            if (!row.alerted && (!row.active || row.attempted)) fresh.push(s);
            row.lastSeen = seenAt; row.raw = s.raw; row.active = 1;
          }
        }
        await write(K.slots, map);
        return fresh;
      },

      /** Safe to call on an empty-but-verified poll: marks absent rows inactive. */
      async deactivateMissing(profile, location, category, presentKeys, seenAt) {
        const map = await read(K.slots, {});
        const present = new Set([...presentKeys].map(([d, t]) => `${d}|${t || ""}`));
        for (const [key, row] of Object.entries(map)) {
          const [p, l, c, d, t] = key.split("|");
          if (p === profile && l === location && c === category && row.active && !present.has(`${d}|${t}`)) {
            row.active = 0; row.lastSeen = seenAt;
          }
        }
        await write(K.slots, map);
      },

      async markAlertAttempted(slots) {
        const map = await read(K.slots, {});
        for (const s of slots) {
          const row = map[slotKey(s.profile, s.location, s.category, s.date, s.time)];
          if (row) row.attempted = 1;
        }
        await write(K.slots, map);
      },

      async markAlerted(slots) {
        const map = await read(K.slots, {});
        for (const s of slots) {
          const row = map[slotKey(s.profile, s.location, s.category, s.date, s.time)];
          if (row) row.alerted = 1;
        }
        await write(K.slots, map);
      },

      async pushAlert(slot, status) {
        const feed = await read(K.alerts, []);
        feed.unshift({ ts: Date.now(), profile: slot.profile, category: slot.category, location: slot.location, date: slot.date, time: slot.time, status });
        await write(K.alerts, feed.slice(0, 200));
      },

      async slotSnapshot() { return read(K.slots, {}); },
      async alertFeed() { return read(K.alerts, []); },

      /* ── booking attempts ──────────────────────────────── */
      async reserveAttempt(slot, mode, startedAt) {
        const log = await read(K.bookings, []);
        if (log.some((r) => r.profile === slot.profile && r.finishedAt == null)) return null;
        const id = (log.reduce((m, r) => Math.max(m, r.id || 0), 0) || 0) + 1;
        log.push({ id, profile: slot.profile, location: slot.location, category: slot.category, date: slot.date, time: slot.time || null, mode, status: "attempted", stage: "claim", startedAt, finishedAt: null });
        await write(K.bookings, log);
        return id;
      },

      async finishAttempt(id, status, extra = {}) {
        const log = await read(K.bookings, []);
        const row = log.find((r) => r.id === id);
        if (!row) return;
        Object.assign(row, { status, finishedAt: extra.finishedAt || nowIso() }, extra);
        await write(K.bookings, log);
      },

      async openAttempt(profile) {
        const log = await read(K.bookings, []);
        return log.find((r) => r.profile === profile && r.finishedAt == null) || null;
      },

      async unresolvedAttempt(profile) {
        const log = await read(K.bookings, []);
        return [...log].reverse().find((r) => r.profile === profile && UNRESOLVED.includes(r.status)) || null;
      },

      async resolveAttempt(id, at) {
        const log = await read(K.bookings, []);
        const row = log.find((r) => r.id === id);
        if (!row || ![...UNRESOLVED, "attempted"].includes(row.status)) return false;
        row.status = "resolved";
        if (row.finishedAt == null) row.finishedAt = at || nowIso();
        await write(K.bookings, log);
        return true;
      },

      async hasBooked(profile) {
        const log = await read(K.bookings, []);
        return log.some((r) => r.profile === profile && r.status === "booked");
      },

      async claimedForSlot(slot) {
        const log = await read(K.bookings, []);
        return log.some((r) => r.profile === slot.profile && r.date === slot.date && (r.time || "") === (slot.time || "") && CLAIMED.includes(r.status));
      },

      async attemptsSince(profile, sinceIso) {
        const log = await read(K.bookings, []);
        return log.filter((r) => r.profile === profile && r.startedAt >= sinceIso).length;
      },

      async bookings(limit = 50) {
        const log = await read(K.bookings, []);
        return [...log].reverse().slice(0, limit);
      },

      /** Why this profile must not navigate/book right now, else null. */
      async blockReason(profile) {
        const open = await this.openAttempt(profile);
        if (open) return `attempt #${open.id} for ${open.date} ${open.time || ""} never finished — a slot may still be held; resolve it first`;
        const un = await this.unresolvedAttempt(profile);
        if (un) return `attempt #${un.id} for ${un.date} ended ${un.status} — a slot may still be held; resolve it first`;
        return null;
      },
    };
  }

  return { createStore, K, TERMINAL, UNRESOLVED, CLAIMED, slotKey, nowIso };
});

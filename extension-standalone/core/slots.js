/* Slot parsing + diffing. Pure JS — shared by content script, service worker,
 * dashboard, and the node test suite. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ABSlots = api;
})(globalThis, function () {
  "use strict";

  const ISO_DATE = /\b(\d{4})-(\d{2})-(\d{2})\b/;
  const DMY_DATE = /\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b/;
  const TIME = /\b(\d{1,2}):(\d{2})\b/;

  /** "2026-12-01 09:00" | "01.12.2026 10:30" | "1/12/26 9:00" -> {date,time|null} or null. */
  function parseSlotLabel(raw) {
    const text = String(raw || "");
    let date = null;
    let m = ISO_DATE.exec(text);
    if (m) {
      date = m[0];
    } else {
      m = DMY_DATE.exec(text);
      if (!m) return null;
      const day = +m[1], month = +m[2];
      let year = +m[3];
      if (year < 100) year += 2000;
      if (month < 1 || month > 12 || day < 1 || day > 31) return null;
      date = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    }
    const t = TIME.exec(text);
    return { date, time: t ? `${String(+t[1]).padStart(2, "0")}:${t[2]}` : null };
  }

  function keyOf(slot) {
    return [slot.profile, slot.location, slot.category, slot.date, slot.time || ""].join("|");
  }

  function sortByDate(slots) {
    return [...slots].sort((a, b) =>
      (a.date + (a.time || "")).localeCompare(b.date + (b.time || ""))
    );
  }

  /** Pick the best new slot: date window, preferred times first, earliest. */
  function selectBookable(slots, prefer) {
    const p = prefer || {};
    const times = p.times || [];
    const candidates = slots.filter((s) => {
      if (p.earliest_date && s.date < p.earliest_date) return false;
      if (p.latest_date && s.date > p.latest_date) return false;
      return true;
    });
    if (!candidates.length) return null;
    let pool = candidates;
    if (times.length) {
      const matching = candidates.filter((s) => times.includes(s.time));
      if (matching.length) pool = matching;
    }
    pool.sort((a, b) => {
      const ta = a.time ? 0 : 1, tb = b.time ? 0 : 1;
      if (ta !== tb) return ta - tb;
      if (a.date !== b.date) return a.date.localeCompare(b.date);
      const ra = times.indexOf(a.time), rb = times.indexOf(b.time);
      if (ra !== rb) return (ra === -1 ? times.length : ra) - (rb === -1 ? times.length : rb);
      return (a.time || "").localeCompare(b.time || "");
    });
    return pool[0];
  }

  return { parseSlotLabel, keyOf, sortByDate, selectBookable };
});

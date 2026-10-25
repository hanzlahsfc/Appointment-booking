/* Poll + assisted-booking orchestration. Pure logic: every browser/storage
 * effect arrives through `env`, so the whole flow is unit-testable in node.
 *
 * env = {
 *   tabs: { get(tabId)->{id,url}|throws, send(tabId,msg)->result|throws },
 *   store: ABState store,
 *   notify(text, title?) -> bool (false = delivery failed, retried next poll),
 *   alarm(message),
 *   claimTab(tabId, profileName), releaseTab(tabId, profileName),
 *   selectedTabId() -> int|null,
 *   selectors, booking: {mode:"off"|"assist", dryRun:bool, maxAttemptsPerHour:int},
 *   now() -> ISO string
 * }
 */
(function (root, factory) {
  const Slots = (typeof module !== "undefined" && module.exports)
    ? require("./slots.js") : root.ABSlots;
  const api = factory(Slots);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ABPoll = api;
})(globalThis, function (Slots) {
  "use strict";

  const REQUIRED_MON = ["vac_select", "category_select", "slot_row"];
  const REQUIRED_BOOK = [...REQUIRED_MON, "reserve_button"];

  function missingSelectors(sel, forBooking) {
    const need = forBooking ? REQUIRED_BOOK : REQUIRED_MON;
    return need.filter((k) => !sel || !sel[k]);
  }

  /** One monitoring poll for a profile. Never throws; returns a status record. */
  async function pollOnce(env, profile) {
    const now = env.now();
    const name = profile.name;
    const rec = { profile: name, status: "ok", error: null, slots: [], fresh: 0, at: now };

    // 1. Never navigate while a slot may be held — survives restarts (DB-backed).
    const blocked = await env.store.blockReason(name);
    if (blocked) return { ...rec, status: "blocked", error: blocked };

    // 2. Resolve and pin the tab.
    const tabId = profile.tabId ?? env.selectedTabId();
    if (tabId == null) return { ...rec, status: "no_tab", error: "no tab selected — pick the portal tab on the dashboard" };
    try {
      env.claimTab(tabId, name);
    } catch (e) {
      return { ...rec, status: "tab_conflict", error: String(e.message || e) };
    }

    try {
      let tab;
      try {
        tab = await env.tabs.get(tabId);
      } catch {
        return { ...rec, status: "tab_closed", error: `tab ${tabId} is closed — pick another` };
      }

      // 3. Probe the page: reachable, past login, not WAF-shelled.
      const probe = await env.tabs.send(tabId, { op: "probe" });
      if (probe.login) return { ...rec, status: "session_expired", error: "portal bounced to sign-in — log in again in that tab" };
      if (probe.waf) return { ...rec, status: "waf_blocked", error: "portal challenge/block shell on this tab" };

      // 4. Selectors: monitoring can't run unconfigured.
      const miss = missingSelectors(env.selectors, env.booking.mode === "assist");
      if (miss.length) {
        return { ...rec, status: "not_configured", error: `selectors missing: ${miss.join(", ")} — run recon and update them` };
      }

      // 5. Apply + verify the picker's VAC/category before trusting any rows.
      const applied = await env.tabs.send(tabId, {
        op: "applyFilters",
        vacSelect: env.selectors.vac_select,
        catSelect: env.selectors.category_select,
        location: profile.location,
        category: profile.category,
      });
      if (!applied.ok) {
        return { ...rec, status: "picker_mismatch", error: applied.error || "picker did not accept the profile's category/location" };
      }

      // 6. Scrape + parse. A matched-but-unparseable page is a failure, not "no slots".
      const { rows } = await env.tabs.send(tabId, { op: "listRows", slotRow: env.selectors.slot_row });
      const parsed = [], bad = [];
      for (const raw of rows || []) {
        const p = Slots.parseSlotLabel(raw);
        if (p) parsed.push({ profile: name, location: profile.location, category: profile.category, date: p.date, time: p.time, raw });
        else bad.push(raw);
      }
      if (rows && rows.length && !parsed.length) {
        return { ...rec, status: "parse_failed", error: `${rows.length} rows matched slot_row but none parsed — check selector/label format` };
      }
      rec.slots = parsed;
      rec.unparsed = bad.length;

      // 7. Diff + retire vanished slots (filters verified ⇒ empty is real).
      const fresh = await env.store.upsertSlots(name, parsed, now);
      await env.store.deactivateMissing(name, profile.location, profile.category, parsed.map((s) => [s.date, s.time]), now);
      rec.fresh = fresh.length;

      // 8. Notify new/alertable slots; failed deliveries retry next poll.
      for (const slot of Slots.sortByDate(fresh)) {
        await env.store.markAlertAttempted([slot]);
        const ok = await env.notify(`NEW SLOT: ${slot.category} @ ${slot.location} — ${slot.date} ${slot.time || ""}`, "Appointment Booker");
        await env.store.pushAlert(slot, ok ? "sent" : "failed");
        if (ok) await env.store.markAlerted([slot]);
      }

      // 9. Assisted booking — off unless master mode + per-profile opt-in.
      if (fresh.length) rec.booking = await maybeBook(env, profile, fresh, now);
      return rec;
    } catch (e) {
      return { ...rec, status: "error", error: `${e && e.name || "Error"}: ${e && e.message || e}` };
    } finally {
      env.releaseTab(tabId, name);
    }
  }

  async function maybeBook(env, profile, fresh, now) {
    const b = env.booking;
    const slot = Slots.selectBookable(fresh, profile.prefer);
    if (!slot) return { status: "no_match" };
    if (!b || b.mode !== "assist" || !profile.autoBook) return { status: "off" };

    if (await env.store.hasBooked(profile.name)) return { status: "skipped", error: "already booked" };
    if (await env.store.claimedForSlot(slot)) return { status: "skipped_duplicate" };
    const hourAgo = new Date(Date.parse(now) - 3600e3).toISOString();
    if (await env.store.attemptsSince(profile.name, hourAgo) >= (b.maxAttemptsPerHour || 4)) {
      return { status: "rate_limited" };
    }
    if (b.dryRun) return { status: "dry_run", slot, error: "would reserve — reserve not clicked" };

    const id = await env.store.reserveAttempt(slot, "assist", now);
    if (id == null) return { status: "skipped_duplicate", error: "another attempt in flight" };

    const tabId = profile.tabId ?? env.selectedTabId();
    let reply;
    try {
      reply = await env.tabs.send(tabId, {
        op: "reserve",
        slotRow: env.selectors.slot_row,
        reserveButton: env.selectors.reserve_button,
        date: slot.date, time: slot.time || "",
      });
    } catch (e) {
      // Click may have landed before the failure — never call this a clean fail.
      await env.store.finishAttempt(id, "aborted_unknown_state", { stage: "reserve", error: String(e.message || e) });
      env.alarm(`CHECK BROWSER — booking of ${slot.date} ${slot.time || ""} is in an unknown state`);
      return { status: "aborted_unknown_state", slot, error: String(e.message || e) };
    }
    if (reply.status === "not_found") {
      await env.store.finishAttempt(id, "slot_gone", { stage: "reserve" });
      return { status: "slot_gone", slot };
    }
    if (reply.status !== "clicked") {
      await env.store.finishAttempt(id, "aborted_unknown_state", { stage: "reserve", error: reply.error || "unknown reply" });
      env.alarm(`CHECK BROWSER — booking of ${slot.date} ${slot.time || ""} is in an unknown state`);
      return { status: "aborted_unknown_state", slot, error: reply.error };
    }
    // Claimed: alarm loud, hand the tab to the human, park this profile.
    await env.store.finishAttempt(id, "needs_human", { stage: "assist" });
    env.alarm(`SLOT CLAIMED — ${slot.category} @ ${slot.location} ${slot.date} ${slot.time || ""} — finish the form NOW`);
    return { status: "needs_human", slot };
  }

  return { pollOnce, missingSelectors };
});

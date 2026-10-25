/* Content script on the portal host. All DOM interaction lives here — the
 * debugger permission is NOT needed: element.click()/dispatchEvent are enough
 * for the picker's links and forms. If real recon proves the portal only reacts
 * to trusted input events, that is when debugger would be reconsidered. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);

  function applySelect(selector, value) {
    const el = $(selector);
    if (!el || !el.options) return { ok: false, error: `select ${selector} not found on page` };
    const opt = [...el.options].find((o) => o.value === value || (o.text || "").trim() === value);
    if (!opt) {
      return { ok: false, error: `${value} not offered — options: ${[...el.options].map((o) => (o.text || "").trim())}` };
    }
    el.value = opt.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }

  function findRow(slotRow, date, time) {
    return [...document.querySelectorAll(slotRow)].find(
      (r) => (r.innerText || "").includes(date) && (!time || (r.innerText || "").includes(time))
    ) || null;
  }

  function handle(msg) {
    switch (msg.op) {
      case "probe":
        return {
          url: location.href,
          login: !!$("input[type=password]"),
          waf: /access denied|error 17|incapsula incident/i.test(document.body ? document.body.innerText : ""),
          title: document.title,
        };
      case "applyFilters": {
        if (msg.vacSelect && msg.location) {
          const r = applySelect(msg.vacSelect, String(msg.location));
          if (!r.ok) return r;
        }
        if (msg.catSelect && msg.category) {
          const r = applySelect(msg.catSelect, String(msg.category));
          if (!r.ok) return r;
        }
        return { ok: true };
      }
      case "listRows":
        return { rows: [...document.querySelectorAll(msg.slotRow)].map((e) => (e.innerText || "").trim()).filter(Boolean) };
      case "reserve": {
        const row = findRow(msg.slotRow, msg.date, msg.time);
        if (!row) return { status: "not_found" };
        const btn = row.querySelector(msg.reserveButton) || $(msg.reserveButton);
        if (!btn) return { status: "not_found", error: "reserve button not in row" };
        btn.scrollIntoView({ block: "center" });
        btn.click(); // may navigate; a throw or missing reply = ambiguous upstream
        return { status: "clicked" };
      }
      default:
        return { error: `unknown op ${msg.op}` };
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
    try {
      respond(handle(msg));
    } catch (e) {
      respond({ error: String((e && e.message) || e) });
    }
    return true;
  });
})();

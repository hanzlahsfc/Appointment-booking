"""Monitoring-path tests: blocked polling, driver-based scraping, alert
retries, tab claiming, label parsing — no browser needed.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

import app.crawler as crawler_module
from app.bridge import HUB, BridgeClient, BridgeError, TabInUse
from app.config import AppConfig, BookingConfig, Config, NotificationsConfig, ProfileConfig
from app.crawler import CrawlerWorker
from app.notifier import Notifier
from app.session import SessionExpired
from app.site import BookingSelectors, PickerMismatch, SiteNotConfigured
from app.store import Store

PROFILE = "Ahmed - Work Permit"

SEL = BookingSelectors(
    vac_select="#vac", category_select="#cat", slot_row=".slot-row",
    reserve_button=".reserve",
)


def _cfg(tmp_path: Path, driver: str = "bridge") -> Config:
    return Config(
        app=AppConfig(dry_run=False, headless=True, driver=driver, poll_interval_seconds=300),
        profiles=[
            ProfileConfig(
                name=PROFILE, email="a@example.com", category="national_d_employment",
                location="lahore", booking_url="https://example.test/appointments",
            )
        ],
        notifications=NotificationsConfig(to=["a@example.com"]),
        booking=BookingConfig(mode="off"),
        root=tmp_path, data_dir=tmp_path, state_dir=tmp_path,
    )


def _profile() -> dict:
    return {
        "name": PROFILE, "email": "a@example.com", "category": "national_d_employment",
        "location": "lahore", "booking_url": "https://example.test/appointments",
        "auto_book": False, "prefer": {}, "tab_id": None,
    }


class _Notifier:
    """Counts sends; can be made to fail."""

    def __init__(self, fail: bool = False):
        self.dry_run = False
        self.fail = fail
        self.sent: list[dict] = []

    def build_subject(self, slot) -> str:
        return "subject"

    async def send(self, slot, auto_book=False) -> bool:
        self.sent.append(dict(slot))
        return not self.fail

    def notify_booking(self, result) -> None: ...


class _Driver:
    """SiteDriver double for the monitoring surface."""

    def __init__(self, selectors=SEL, rows=(), tab_id=7):
        self.selectors = selectors
        self.rows = list(rows)
        self.tab_id = tab_id
        self.login = False
        self.waf_exc = None
        self.filter_exc = None
        self.calls: list[str] = []
        self.filters = None

    async def open_booking_page(self, url): self.calls.append("open")
    async def wait_for_waf(self):
        self.calls.append("waf")
        if self.waf_exc:
            raise self.waf_exc

    async def is_login_page(self): return self.login

    async def apply_filters(self, location, category):
        self.calls.append("filters")
        self.filters = (location, category)
        if self.filter_exc:
            raise self.filter_exc

    async def list_slots(self):
        self.calls.append("slots")
        return list(self.rows)

    async def locate(self, slot): return True
    async def reserve(self, slot): self.calls.append("reserve")
    async def read_tokens(self): return {}
    async def fill_applicant(self, data): pass
    async def submit(self): pass
    async def confirmation_ref(self): return "REF-1"
    async def screenshot(self, path): pass
    async def content(self): return "<html/>"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def _worker(cfg, store, notifier):
    return CrawlerWorker(cfg, _profile(), store, notifier, dashboard=None, demo=False)


# ── block reasons (poll must not navigate while a slot may be held) ────────


def test_unresolved_booking_blocks_polling(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    assert w._poll_block_reason() is None
    bid = store.reserve_booking_attempt(
        {"profile": PROFILE, "location": "lahore", "category": "c",
         "date": "2026-12-01", "time": "09:00"}, "assist", "t1")
    store.finish_booking(bid, "needs_human", finished_at="t2")
    assert "may still be held" in w._poll_block_reason()


def test_stale_in_flight_attempt_blocks_polling(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    store.reserve_booking_attempt(
        {"profile": PROFILE, "location": "lahore", "category": "c",
         "date": "2026-12-01", "time": None}, "assist", "t1")
    assert "never finished" in w._poll_block_reason()


def test_blocked_worker_never_polls(store, tmp_path, monkeypatch):
    """While blocked, the loop rechecks instead of navigating the tab."""
    monkeypatch.setattr(crawler_module, "BLOCKED_RECHECK_SECONDS", 0.02)
    w = _worker(_cfg(tmp_path), store, _Notifier())
    store.reserve_booking_attempt(
        {"profile": PROFILE, "location": "lahore", "category": "c",
         "date": "2026-12-01", "time": "09:00"}, "assist", "t1")
    store.finish_booking(store.recent_bookings()[0]["id"], "needs_human", finished_at="t2")

    polled = []

    async def fake_poll():
        polled.append(1)

    monkeypatch.setattr(w, "_poll_once_bridge", fake_poll)

    async def scenario():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert polled == []


def test_resolving_unblocks_the_loop(store, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler_module, "BLOCKED_RECHECK_SECONDS", 0.02)
    w = _worker(_cfg(tmp_path), store, _Notifier())
    bid = store.reserve_booking_attempt(
        {"profile": PROFILE, "location": "lahore", "category": "c",
         "date": "2026-12-01", "time": "09:00"}, "assist", "t1")
    store.finish_booking(bid, "needs_human", finished_at="t2")
    assert w._poll_block_reason() is not None
    assert store.resolve_booking(bid, resolved_at="t3")
    assert w._poll_block_reason() is None


# ── driver-based poll ────────────────────────────────────────────────────


def test_poll_scrapes_rows_after_verified_filters(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    driver = _Driver(rows=["2026-12-01 09:00", "01.12.2026 10:30"])
    asyncio.run(w._poll_via_driver(driver))

    assert driver.filters == ("lahore", "national_d_employment")
    rows = store._conn.execute("SELECT date, time FROM slot_state ORDER BY time").fetchall()
    assert [(r["date"], r["time"]) for r in rows] == [
        ("2026-12-01", "09:00"), ("2026-12-01", "10:30")
    ]


def test_poll_releases_the_tab_claim(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    asyncio.run(w._poll_via_driver(_Driver()))
    assert HUB.tab_owner(7) is None


def test_conflicting_tab_claim_is_refused(store, tmp_path):
    HUB.claim_tab(7, "other-profile")
    try:
        with pytest.raises(TabInUse, match="already being driven"):
            HUB.claim_tab(7, PROFILE)
    finally:
        HUB.reset()


def test_login_page_reports_session_expired(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    driver = _Driver()
    driver.login = True
    with pytest.raises(SessionExpired, match="sign-in"):
        asyncio.run(w._poll_via_driver(driver))
    msg = w._explain_error(SessionExpired("the portal bounced to sign-in"))
    assert "bounced" in msg


def test_picker_mismatch_is_reported(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    driver = _Driver()
    driver.filter_exc = PickerMismatch("category 'x' is not offered")
    with pytest.raises(PickerMismatch, match="not offered"):
        asyncio.run(w._poll_via_driver(driver))
    assert "not offered" in w._explain_error(PickerMismatch("x is not offered"))


def test_unconfigured_selectors_block_scrape(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    driver = _Driver(selectors=BookingSelectors())
    with pytest.raises(SiteNotConfigured, match="not configured"):
        asyncio.run(w._poll_via_driver(driver))
    assert "not configured" in w._explain_error(SiteNotConfigured("x is not configured"))
    assert store._conn.execute("SELECT COUNT(*) n FROM slot_state").fetchone()["n"] == 0


# ── store behaviours ─────────────────────────────────────────────────────


def _slot(date="2026-12-01", time="09:00"):
    return {"profile": PROFILE, "location": "lahore",
            "category": "national_d_employment", "date": date, "time": time}


def test_nullable_time_dedupes(store):
    """time=None and time='' are the same slot — NULL must not split the PK."""
    s1 = _slot(time=None)
    s2 = dict(s1, time="")
    new = store.upsert_slots([s1], "t1")
    store.mark_alerted(new)
    assert store.upsert_slots([s2], "t2") == []
    n = store._conn.execute(
        "SELECT COUNT(*) n FROM slot_state WHERE date='2026-12-01'"
    ).fetchone()["n"]
    assert n == 1


def test_empty_poll_deactivates_known_slots(store):
    """Once filters verified, 'no rows' means the slots are gone."""
    new = store.upsert_slots([_slot()], "t1")
    store.mark_alerted(new)
    store.deactivate_missing(PROFILE, "lahore", "national_d_employment", set(), "t2")
    row = store._conn.execute("SELECT active FROM slot_state").fetchone()
    assert row["active"] == 0


def test_failed_alert_retries_on_next_poll(store):
    """Send fails → alerted=0/attempted=1 → same slot is alertable again."""
    new = store.upsert_slots([_slot()], "t1")
    store.mark_alert_attempted(new)
    assert store.upsert_slots([_slot()], "t2") == [_slot()]
    store.mark_alerted([_slot()])
    assert store.upsert_slots([_slot()], "t3") == []


def test_dispatch_marks_attempt_then_alerted(store, tmp_path):
    notifier = _Notifier()
    w = _worker(_cfg(tmp_path), store, notifier)
    asyncio.run(w._dispatch_alert(_slot()))
    row = store._conn.execute(
        "SELECT alerted, alert_attempted FROM slot_state"
    ).fetchone()
    # dispatched without a prior upsert row → nothing to mark; emulate real flow
    store.upsert_slots([_slot()], "t0")
    asyncio.run(w._dispatch_alert(_slot()))
    row = store._conn.execute(
        "SELECT alerted, alert_attempted FROM slot_state"
    ).fetchone()
    assert (row["alerted"], row["alert_attempted"]) == (1, 1)
    assert len(notifier.sent) == 2


def test_failed_send_is_retried_by_next_poll(store, tmp_path):
    notifier = _Notifier(fail=True)
    w = _worker(_cfg(tmp_path), store, notifier)
    driver = _Driver(rows=["2026-12-01 09:00"])
    asyncio.run(w._poll_via_driver(driver))
    asyncio.run(asyncio.sleep(0))  # let the dispatched alert task run
    notifier.fail = False
    asyncio.run(w._poll_via_driver(driver))
    asyncio.run(asyncio.sleep(0))
    assert len(notifier.sent) >= 2  # once failed, once retried


# ── label parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,want", [
    ("2026-12-01 09:00", ("2026-12-01", "09:00")),
    ("01.12.2026 10:30", ("2026-12-01", "10:30")),
    ("1/12/26 9:00", ("2026-12-01", "09:00")),
    ("Available: 2026-12-05", ("2026-12-05", None)),
])
def test_slot_label_formats(raw, want):
    assert CrawlerWorker._parse_slot_label(raw) == want


@pytest.mark.parametrize("raw", ["no date here", "32.13.2026 09:00", "99:99", ""])
def test_slot_label_rejects_garbage(raw):
    assert CrawlerWorker._parse_slot_label(raw) is None


# ── bridge-mode poll through the real hub ────────────────────────────────


class _FakeExt:
    """Emulates the extension's replies to driver JS over a BridgeClient."""

    def __init__(self, rows):
        self.sent = []
        self.rows = rows
        self.client = BridgeClient(client_id="t", send_raw=self._send)

    async def _send(self, raw):
        import json
        msg = json.loads(raw)
        self.sent.append(msg)
        expr = str(msg.get("params", {}).get("expression", ""))
        if msg.get("op") == "command" and "readyState" in expr:
            value = "complete"
        elif "input[type=password]" in expr:
            value = False
        elif "el.options" in expr:
            value = {"found": True, "applied": True}
        elif "slot-row" in expr:
            value = list(self.rows)
        else:
            value = {}
        asyncio.ensure_future(
            self.client.handle_message(
                {"id": msg.get("id"), "ok": True, "result": {"result": {"value": value}}}
            )
        )


def test_end_to_end_poll_via_hub(store, tmp_path):
    """Register a fake extension, run one real bridge poll, verify storage."""
    cfg = _cfg(tmp_path)
    sel_file = tmp_path / "selectors.json"
    sel_file.write_text(
        '{"vac_select":"#vac","category_select":"#cat",'
        '"slot_row":".slot-row","reserve_button":".reserve"}'
    )
    cfg.booking.selectors_file = sel_file
    w = _worker(cfg, store, _Notifier())
    ext = _FakeExt(rows=["2026-12-10 11:00"])
    HUB.register(ext.client)

    async def scenario():
        await ext.client.handle_message(
            {"type": "hello", "protocol": 1, "version": "t", "tabId": 42}
        )
        await w._poll_once_bridge()

    try:
        asyncio.run(scenario())
        rows = store._conn.execute("SELECT date, time FROM slot_state").fetchall()
        assert [(r["date"], r["time"]) for r in rows] == [("2026-12-10", "11:00")]
        assert HUB.tab_owner(42) is None
    finally:
        HUB.reset()


def test_poll_without_extension_reports_clearly(store, tmp_path):
    w = _worker(_cfg(tmp_path), store, _Notifier())
    HUB.reset()
    asyncio.run(w._poll_once_bridge())  # must not raise
    # nothing was scraped, nothing was written
    assert store._conn.execute("SELECT COUNT(*) n FROM slot_state").fetchone()["n"] == 0


# ── booking disconnect + latency ─────────────────────────────────────────


def test_disconnect_mid_booking_is_unknown_state(store, tmp_path):
    """Extension dies between reserve and submit: the slot may be held."""
    from app.booking import BookingAgent, BookingStatus
    from app.site import BridgeSiteDriver

    cfg = _cfg(tmp_path)
    cfg.booking.mode = "auto"
    cfg.booking.dry_run = False
    cfg.profiles[0].auto_book = True
    profile = _profile()
    profile["auto_book"] = True
    profile["applicant_file"] = "a"
    (tmp_path / "applicants").mkdir(exist_ok=True)
    (tmp_path / "applicants" / "a.json").write_text('{"passport":"P1"}')

    class _Dying(_Driver):
        async def submit(self):
            raise BridgeError("extension disconnected mid-submit")

    selectors = BookingSelectors(
        vac_select="#vac", category_select="#cat", slot_row=".slot-row",
        reserve_button=".reserve", applicant_fields={"passport": "#p"},
        submit_button="#sub", confirmation_marker="#conf",
    )
    agent = BookingAgent(cfg, profile, store)
    result = asyncio.run(agent.book(
        {"profile": PROFILE, "location": "lahore", "category": "national_d_employment",
         "date": "2026-12-01", "time": "09:00"},
        _Dying(selectors=selectors),
    ))
    assert result.status == BookingStatus.ABORTED_UNKNOWN_STATE.value
    assert result.claimed


def test_detection_to_reservation_latency_is_fast(store, tmp_path):
    """Measure it; the claim must happen well inside any real hold window."""
    from app.booking import BookingAgent

    cfg = _cfg(tmp_path)
    cfg.booking.mode = "assist"
    cfg.booking.dry_run = False
    cfg.profiles[0].auto_book = True
    profile = _profile()
    profile["auto_book"] = True

    agent = BookingAgent(cfg, profile, store)
    slot = {"profile": PROFILE, "location": "lahore",
            "category": "national_d_employment", "date": "2026-12-01", "time": "09:00"}
    driver = _Driver()

    t0 = time.perf_counter()
    result = asyncio.run(agent.book(slot, driver))
    elapsed = time.perf_counter() - t0

    assert result.status == "needs_human"
    assert "reserve" in driver.calls
    assert elapsed < 5.0  # in-process; the wire adds only bridge round-trips
    print(f"\n[latency] detect->reserve (in-process driver): {elapsed * 1000:.0f} ms")

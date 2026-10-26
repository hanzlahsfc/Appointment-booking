"""Regression coverage for deployment blockers observed on the operator host."""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.hosted.store import HostedStore, SCHEMA
from app.hosted.manager import ClientBrowser
from app.hosted.monitor import HostedMonitor
from app.config import HostedConfig
from app.site import SiteNotConfigured

def test_legacy_database_without_password_default(tmp_path):
    db = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA.replace("password_hash   TEXT NOT NULL DEFAULT ''", 'password_hash   TEXT NOT NULL'))
    conn.close()
    store = HostedStore(db)
    try:
        client, code = store.add_client('a', 'A', 'client:a', 'https://portal.example', 'work', 'lahore', ['https://portal.example'])
        assert store.authenticate_code(code)['id'] == client['id']
    finally:
        store.close()

def test_captcha_iframe_allowed_but_top_navigation_blocked():
    async def scenario():
        browser = ClientBrowser({'id': 1, 'allowed_origins': ['https://portal.example']}, HostedConfig())
        for parent, allowed in [(object(), True), (None, False)]:
            route = SimpleNamespace(request=SimpleNamespace(url='https://newassets.hcaptcha.com/captcha/test.html', frame=SimpleNamespace(parent_frame=parent), is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route_guard(route)
            assert route.continue_.await_count == int(allowed)
            assert route.abort.await_count == int(not allowed)
    asyncio.run(scenario())

def test_unparseable_slots_are_not_empty_success():
    monitor = object.__new__(HostedMonitor)
    monitor.profile_name = 'client:a'
    monitor.client = {'location': 'lahore', 'category': 'work'}
    with pytest.raises(SiteNotConfigured):
        monitor._normalize(['something that is not a date'])
    assert monitor._normalize([]) == []

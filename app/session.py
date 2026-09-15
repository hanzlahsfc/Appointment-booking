"""Login helper + storage_state persistence."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from .browser import IncapsulaHandler, get_page
from .util import slugify

logger = logging.getLogger(__name__)

LOGIN_URL = "https://pk-gr-services.gvcworld.eu/login/PK/en"
LOGGED_IN_EXPECT = "/en/account"  # adjust after recon (§11)


class LoginError(Exception):
    pass


def load_credentials(path: str | Path) -> dict:
    """Load <data>/creds/<profile>.json — {username, password}."""
    p = Path(path)
    if not p.exists():
        raise LoginError(f"credential file not found: {p}")
    data = json.loads(p.read_text())
    if not data.get("username") or not data.get("password"):
        raise LoginError(f"credential file missing username/password: {p}")
    return data


def storage_state_path(state_dir: Path, profile_name: str) -> Path:
    return state_dir / f"{slugify(profile_name)}.json"


def has_storage_state(state_dir: Path, profile_name: str) -> bool:
    return storage_state_path(state_dir, profile_name).exists()


async def login(page: Page, creds: dict, expected_redirect: str = None) -> None:
    """Perform the login flow from the login page.

    Assumes `page` is already on the login page. Waits for Incapsula, fills
    credentials, submits, waits for redirect. Throws LoginError on failure.
    """
    inc = IncapsulaHandler()
    await inc.wait_for_clear(page, expect_text="login")

    try:
        await page.fill("input[name='username'], input#edit-name, input#email", creds["username"])
        await page.fill("input[name='password'], input#edit-pass", creds["password"])
    except Exception as e:
        raise LoginError(f"could not locate credential fields: {e}") from e

    submit = page.locator("button[type='submit'], input[type='submit'], .form-submit")
    try:
        await submit.first.click(timeout=10_000)
    except Exception as e:
        raise LoginError(f"submit failed: {e}") from e

    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass

    target = (expected_redirect or LOGGED_IN_EXPECT)
    if target not in page.url:
        raise LoginError(f"login did not redirect (expected {target}, got {page.url})")


async def ensure_logged_in(
    context: BrowserContext,
    creds_file: Path,
    profile_name: str,
    state_path: Path,
    expected_redirect: str | None = None,
) -> None:
    """If no storage_state exists, drive a headful login once and persist it."""
    if state_path.exists():
        return
    logger.info("no storage_state for %s; starting one-time login", profile_name)
    page = await get_page(context)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    creds = load_credentials(creds_file)
    await login(page, creds, expected_redirect)
    await context.storage_state(path=state_path)
    logger.info("persisted storage_state for %s -> %s", profile_name, state_path)


async def check_session_still_valid(page: Page, login_marker: str = "login/PK/en") -> bool:
    """True if the page has NOT bounced back to the sign-in page."""
    return login_marker not in page.url
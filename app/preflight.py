"""Egress preflight.

The portal sits behind Imperva/Incapsula. A non-browser client cannot solve its
JS challenge, so it gets a 403 with `edet=17` instead of clearance — which looks
identical to a dead host if you only log the status code. This module turns that
response into a clear verdict so a blocked egress fails loudly at startup
instead of silently failing every poll.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Markers that identify the Incapsula block shell (case-insensitive).
WAF_MARKERS = (
    "_incapsula_resource",
    "incapsula incident",
    "access denied",
    "error 17",
    "edet=17",
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


@dataclass
class PreflightResult:
    reachable: bool
    blocked: bool
    status_code: int | None
    detail: str
    snippet: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and not self.blocked


def interpret(status_code: int | None, body: str | None) -> PreflightResult:
    """Pure verdict from a response — unit-testable without network."""
    low = (body or "").lower()
    marker = next((m for m in WAF_MARKERS if m in low), None)

    if status_code in (200, 302):
        if marker:
            return PreflightResult(
                False,
                True,
                status_code,
                f"portal answered {status_code} but the body is the WAF shell ({marker})",
                (body or "")[:400],
            )
        return PreflightResult(True, False, status_code, f"portal reachable ({status_code})")

    if status_code == 403:
        return PreflightResult(
            False,
            True,
            status_code,
            "HTTP 403 from the WAF — this egress has no Incapsula clearance "
            "(a browser can pass the JS challenge; curl cannot). Verify from a "
            "real browser session, or use a residential network.",
            (body or "")[:400],
        )

    if marker:
        return PreflightResult(
            False,
            True,
            status_code,
            f"WAF shell detected (status={status_code}, marker={marker})",
            (body or "")[:400],
        )

    if status_code is None:
        return PreflightResult(False, False, None, "no response (network/DNS error)")

    return PreflightResult(
        status_code < 500,
        False,
        status_code,
        f"unexpected status {status_code}",
        (body or "")[:400],
    )


async def check_reachability(
    url: str,
    timeout: float = 20.0,
    user_agent: str = DEFAULT_UA,
) -> PreflightResult:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": user_agent}
        ) as client:
            response = await client.get(url)
        return interpret(response.status_code, response.text)
    except httpx.HTTPError as exc:
        logger.warning("preflight network error for %s: %s", url, exc)
        return PreflightResult(False, False, None, f"network error: {exc}")


async def check_session(page, login_marker: str = "login") -> bool:
    """True when the page is past the sign-in screen (warm authenticated profile)."""
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if login_marker in url:
        return False
    try:
        return await page.locator("input[type=password]").count() == 0
    except Exception:
        return True

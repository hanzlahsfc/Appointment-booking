"""Page adapter over the project's OWN WebSocket bridge (app/bridge.py).

Lets GvcwSiteDriver run through the bundled extension's ws:// connection —
no devtools-extension repo, no Node.js, no native host. Same duck-typed
Playwright surface and safety gates as cdp_bridge.BridgePage: explicit tab,
per-operation origin check, session/challenge stops, read-only default.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from playwright.async_api import TimeoutError as PwTimeout
except ImportError:                        # playwright not installed —
    class PwTimeout(Exception):            # WS-extension path doesn't need it
        pass

from .bridge import BridgeClient, BridgeError, BridgeTimeout

logger = logging.getLogger(__name__)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class WsPage:
    """Duck-typed Playwright Page over BridgeClient (the app's WS bridge)."""

    def __init__(
        self,
        client: BridgeClient,
        allowed_origins: list[str],
        *,
        allow_writes: bool = False,
        pinned_tab: int | None = None,
    ):
        self._c = client
        self.pinned_tab = pinned_tab
        self.allowed = {_origin(o) for o in allowed_origins}
        self.allow_writes = allow_writes
        self._url: str | None = None

    # ── safety gate ──────────────────────────────────────────────────────

    async def _gate(self, *, writes: bool = False, session: bool = True) -> str:
        if writes and not self.allow_writes:
            raise BridgeError(
                "read-only page — rebuild with allow_writes (explicit "
                "operator action)")
        # If the operator changed the popup selection (or the extension
        # reconnected to a different tab), STOP — never follow another tab.
        if self.pinned_tab is not None and self._c.tab_id != self.pinned_tab:
            raise BridgeError(
                f"selected tab changed ({self._c.tab_id} != "
                f"{self.pinned_tab}) — automation paused; re-pair to continue")
        url = await self._c.evaluate("location.href", timeout=10) or ""
        if _origin(url) not in self.allowed:
            raise BridgeError(
                f"tab left the allowed origin set: {url}")
        self._url = url
        if session:
            await self._check_session()
        return url

    async def _check_session(self) -> None:
        # reCAPTCHA iframe on /add is normal — challenge = Incapsula markers
        # AND no booking form. Never bypass; surface it for the operator.
        st = await self._c.evaluate(
            "JSON.stringify({"
            "login: !!(document.querySelector('#username')"
            "&&document.querySelector('#password')),"
            "unauth: document.title.startsWith('401')||"
            "(document.body&&document.body.innerText"
            ".includes('not verify that you are authorized')),"
            "challenge: !!document.querySelector("
            "\"iframe[src*='Incapsula'],iframe[src*='incapsula'],"
            "#incapsula,#challenge-form,#challenge-running,"
            "div[class*='incapsula']\")"
            "&&!document.querySelector('#type,#datefrom,#btn-search')})",
            timeout=15,
        ) or "{}"
        st = json.loads(st)
        if st.get("login") or st.get("unauth"):
            raise BridgeError("session lost — sign back in, then re-pair")
        if st.get("challenge"):
            raise BridgeError("access challenge on the tab — solve by hand")

    async def _eval(self, expression: str, arg: Any = ...) -> Any:
        if arg is not ...:
            expression = f"({expression})({json.dumps(arg)})"
        else:
            s = expression.lstrip()
            if (s.startswith("(") or s.startswith("function")
                    or s.startswith("async")) and not s.endswith("()"):
                expression = f"({expression})()"
        return await self._c.evaluate(expression, timeout=30)

    # ── Playwright-shaped surface ────────────────────────────────────────

    @property
    def url(self) -> str:
        return self._url or "about:blank"

    async def evaluate(self, expression: str, arg: Any = ...) -> Any:
        self._url = await self._gate()
        return await self._eval(expression, arg)

    async def goto(self, url: str, wait_until: str = "domcontentloaded",
                   timeout: int = 30_000) -> None:
        if _origin(url) not in self.allowed:
            raise BridgeError(f"goto outside allowlist: {url}")
        await self._gate(writes=True, session=False)
        await self._c.command("Page.navigate", {"url": url})
        await self._wait_ready(timeout)
        self._url = url

    async def _wait_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            if await self._eval("document.readyState") in ("interactive", "complete"):
                return
            await asyncio.sleep(0.25)
        raise PwTimeout(f"page never became ready within {timeout}ms")

    async def click(self, selector: str) -> None:
        await self._gate(writes=True)
        await self._trusted_click(selector)
        self._url = await self._gate()

    async def _trusted_click(self, selector: str) -> None:
        box = await self._eval(
            "(() => { const el = document.querySelector(ARG_SEL);"
            " if(!el) return null; el.scrollIntoView({block:'center'});"
            " const r = el.getBoundingClientRect();"
            " if (!r.width && !r.height) return null;"
            " return {x: Math.round(r.x + r.width/2),"
            "         y: Math.round(r.y + r.height/2)}; })()".replace(
                "ARG_SEL", repr(selector))
        )
        if not box:
            raise PwTimeout(f"no element for click: {selector}")
        for t in ("mousePressed", "mouseReleased"):
            await self._c.command(
                "Input.dispatchMouseEvent",
                {"type": t, "x": box["x"], "y": box["y"],
                 "button": "left", "clickCount": 1})

    def locator(self, selector: str) -> "WsLocator":
        return WsLocator(self, selector)

    async def wait_for_selector(self, selector: str, state: str = "visible",
                                timeout: int = 15_000) -> None:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            await self._gate()
            found = await self._eval(
                "(() => { const el = document.querySelector(ARG_SEL);"
                " if(!el) return false; return ARG_VIS ? !!el.offsetParent : true; })()"
                .replace("ARG_SEL", repr(selector))
                .replace("ARG_VIS", "true" if state == "visible" else "false"))
            if found:
                return
            await asyncio.sleep(0.3)
        raise PwTimeout(f"wait_for_selector {selector} timed out")

    async def wait_for_function(self, js: str, timeout: int = 15_000) -> None:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            await self._gate()
            if await self._eval(js):
                return
            await asyncio.sleep(0.3)
        raise PwTimeout("wait_for_function timed out")

    async def wait_for_url(self, glob: str, timeout: int = 15_000) -> None:
        import fnmatch
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            url = await self._gate()
            self._url = url
            if fnmatch.fnmatch(url, glob):
                return
            await asyncio.sleep(0.3)
        raise PwTimeout(f"wait_for_url {glob} timed out (url={self._url})")

    async def screenshot(self, path: Path) -> None:
        await self._gate()
        res = await self._c.command("Page.captureScreenshot", {"format": "png"})
        data = (res or {}).get("data")
        if not data:
            raise BridgeError("captureScreenshot returned no data")
        Path(path).write_bytes(base64.b64decode(data))

    async def content(self) -> str:
        return await self.evaluate("document.documentElement.outerHTML") or ""


class WsLocator:
    def __init__(self, page: WsPage, selector: str):
        self.page = page
        self.selector = selector

    def _js(self, body: str) -> str:
        return ("(() => { const el = document.querySelector(ARG_SEL);"
                + body + ")()").replace("ARG_SEL", repr(self.selector))

    async def count(self) -> int:
        await self.page._gate()
        return await self.page._eval(self._js(" return el ? 1 : 0;")) or 0

    async def is_visible(self) -> bool:
        await self.page._gate()
        return bool(await self.page._eval(
            self._js(" return !!(el && el.offsetParent);")))

    async def is_checked(self) -> bool:
        await self.page._gate()
        return bool(await self.page._eval(
            self._js(" return !!(el && el.checked);")))

    async def input_value(self) -> str:
        await self.page._gate()
        return await self.page._eval(self._js(" return el ? el.value : '';")) or ""

    async def check(self) -> None:
        await self.page._gate(writes=True)
        await self.page._eval(self._js(
            " if(el && !el.checked){ el.click(); } return true;"))
        self.page._url = await self.page._gate()

    async def click(self) -> None:
        await self.page._gate(writes=True)
        await self.page._trusted_click(self.selector)
        self.page._url = await self.page._gate()

    @property
    def first(self) -> "WsLocator":
        return self

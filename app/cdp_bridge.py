"""CDP-Bridge-backed page — drive an operator's signed-in Chrome tab through
the devtools-extension bridge (HTTP API on localhost:1232) instead of a
hosted Playwright browser.

Hard rules baked into the transport:

  * ONE explicit tab id, verified before EVERY operation — the shim never
    selects or falls back to another tab. Tab gone = BridgeDisconnected.
  * The tab's URL origin must stay inside the caller-supplied allowlist.
    Any other navigation raises BridgeNavRejected and stops the run.
  * Logout (login form / 401 page) raises BridgeSessionLost; a visible
    challenge raises BridgeChallenge. Ambiguous submit outcomes are never
    retried by this layer.
  * Mutating calls (trusted clicks, OTP, submit) are refused unless the
    page was constructed with allow_writes=True — the recon default is
    strictly read-only.

Nothing here copies cookies, credentials, or profiles: only DOM reads,
CDP navigation within the allowlist, and trusted input on the named tab.

NOTE: app/bridge.py is the project's own WebSocket bridge for the legacy
select-based flow — this module talks to the devtools-extension HTTP
bridge (the extension the operator actually runs) and provides the
Playwright-shaped page surface that GvcwSiteDriver needs.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from playwright.async_api import TimeoutError as PwTimeout
except ImportError:                        # playwright not installed —
    class PwTimeout(Exception):            # extension path doesn't need it
        pass

logger = logging.getLogger(__name__)

BRIDGE_BASE = "http://localhost:1232"


class BridgeError(RuntimeError):
    pass


class BridgeDisconnected(BridgeError):
    """The named tab vanished, detached, or the host went away."""


class BridgeNavRejected(BridgeError):
    """The tab navigated outside the caller's origin allowlist."""


class BridgeSessionLost(BridgeError):
    """Login form or a bare 401 page — the signed-in session is gone."""


class BridgeChallenge(BridgeError):
    """A CAPTCHA/Incapsula challenge is on screen — stop for a human."""


class BridgeEvalError(BridgeError):
    """Runtime.evaluate raised on the page (exceptionDetails)."""


class BridgeReadOnly(BridgeError):
    """A mutating call reached a page built without allow_writes."""


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


_UNSET = object()


class BridgeClient:
    """Thin wrapper for the devtools-extension HTTP bridge."""

    def __init__(self, base: str = BRIDGE_BASE):
        self.base = base

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as h:
                r = await h.get(f"{self.base}/health")
            return bool(r.json().get("ok"))
        except Exception:
            return False

    async def tabs(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(f"{self.base}/tabs")
            return r.json().get("result", [])
        except Exception as exc:
            raise BridgeDisconnected(str(exc))

    async def page(self, tab_id: int, allowed_origins: list[str],
                   allow_writes: bool = False) -> "BridgePage":
        return BridgePage(
            self, tab_id, allowed_origins, allow_writes=allow_writes)


class BridgePage:
    """Duck-typed Playwright Page over the CDP Bridge HTTP API."""

    def __init__(
        self,
        client: BridgeClient,
        tab_id: int,
        allowed_origins: list[str],
        *,
        allow_writes: bool = False,
    ):
        self._c = client
        self.tab_id = tab_id
        self.allowed = {_origin(o) for o in allowed_origins}
        self.allow_writes = allow_writes
        self._url: str | None = None

    # ── transport + safety gate ──────────────────────────────────────────

    async def _tabs(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(f"{self._c.base}/tabs")
            return r.json().get("result", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeDisconnected(f"bridge host unreachable: {exc}")

    async def _command(self, method: str, params: dict | None = None) -> Any:
        payload = {"tabId": self.tab_id, "method": method, "params": params or {}}
        try:
            async with httpx.AsyncClient(timeout=30) as h:
                r = await h.post(f"{self._c.base}/command", json=payload)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeDisconnected(f"{method}: {exc}")
        if "error" in data:
            err = str(data["error"])
            if "not attached" in err.lower() or "no tab" in err.lower():
                raise BridgeDisconnected(err)
            raise BridgeEvalError(f"{method}: {err}")
        return data.get("result")

    async def _gate(self, *, writes: bool = False, session: bool = True) -> str:
        """Verify tab presence + origin before every operation. Returns the
        tab's current URL."""
        if writes and not self.allow_writes:
            raise BridgeReadOnly(
                "this BridgePage is read-only — rebuild with allow_writes "
                "(the operator's explicit --enable-booking action)")
        tabs = await self._tabs()
        tab = next((t for t in tabs if t.get("tabId") == self.tab_id), None)
        if tab is None:
            raise BridgeDisconnected(f"tab {self.tab_id} is gone")
        url = tab.get("url", "")
        if _origin(url) not in self.allowed:
            raise BridgeNavRejected(
                f"tab {self.tab_id} left the allowed origin set: {url}")
        if session:
            await self._check_session()
        return url

    async def _check_session(self) -> None:
        # A plain reCAPTCHA iframe is NORMAL — the real /appointments/add
        # form embeds one for final submission, so it must never flag a
        # challenge. A genuine access-challenge page has Incapsula markers
        # AND no booking form (#type/#datefrom/#btn-search). When a real
        # challenge does appear we stop, we never solve or bypass it.
        res = await self._command(
            "Runtime.evaluate",
            {
                "expression": (
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
                    "&&!document.querySelector('#type,#datefrom,#btn-search')})"
                ),
                "returnByValue": True,
            },
        ) or {}
        if res.get("exceptionDetails"):
            raise BridgeEvalError(
                f"session check eval failed: {res['exceptionDetails']}"[:200])
        val = (res.get("result") or {}).get("value") or "{}"
        st = json.loads(val)
        if st.get("login") or st.get("unauth"):
            raise BridgeSessionLost(
                "login form / 401 on the tab — sign back in, then re-run")
        if st.get("challenge"):
            raise BridgeChallenge("challenge frame on the tab — solve it first")

    async def _eval(self, expression: str, arg: Any = _UNSET) -> Any:
        """Runtime.evaluate with exceptionDetails → BridgeEvalError."""
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if arg is not _UNSET:
            # Playwright semantics: the expression IS the function, called
            # with the arg — evaluate("sel => fn(sel)", value).
            params["expression"] = f"({expression})({json.dumps(arg)})"
        else:
            stripped = expression.lstrip()
            # CDP evaluates a bare `() => ...` to a function OBJECT (returns
            # undefined), while Playwright invokes it. Wrap function literals
            # so callers written for Playwright behave the same here.
            if ((stripped.startswith("(") or stripped.startswith("function")
                    or stripped.startswith("async"))
                    and not stripped.endswith("()")):
                params["expression"] = f"({expression})()"
        res = await self._command("Runtime.evaluate", params) or {}
        inner = res.get("result") or res
        if res.get("exceptionDetails") or inner.get("exceptionDetails"):
            raise BridgeEvalError(
                str(res.get("exceptionDetails") or inner.get("exceptionDetails"))[:300])
        return inner.get("value")

    # ── Playwright-shaped surface (the subset GvcwSiteDriver uses) ───────

    @property
    def url(self) -> str:  # cached per gate; call sites read page.url
        return self._url or "about:blank"

    async def fresh_url(self) -> str:
        return await self._gate(session=False)

    async def goto(self, url: str, wait_until: str = "domcontentloaded",
                   timeout: int = 30_000) -> None:
        if _origin(url) not in self.allowed:
            raise BridgeNavRejected(f"goto outside allowlist: {url}")
        await self._gate(writes=True, session=False)
        await self._command("Page.navigate", {"url": url})
        await self._wait_ready(timeout)
        self._url = url

    async def _wait_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            ready = await self._eval("document.readyState")
            if ready in ("interactive", "complete"):
                return
            await asyncio.sleep(0.25)
        raise PwTimeout(f"page never became ready within {timeout}ms")

    async def evaluate(self, expression: str, arg: Any = _UNSET) -> Any:
        self._url = await self._gate()
        return await self._eval(expression, arg)

    async def click(self, selector: str) -> None:
        await self._gate(writes=True)
        await self._trusted_click(selector)
        self._url = await self.fresh_url()

    async def _trusted_click(self, selector: str) -> None:
        box = await self._eval(
            "(() => { const el = document.querySelector(ARG_SEL);"
            " if(!el) return null;"
            " el.scrollIntoView({block:'center'});"
            " const r = el.getBoundingClientRect();"
            " if (!r.width && !r.height) return null;"
            " return {x: Math.round(r.x + r.width/2),"
            "         y: Math.round(r.y + r.height/2)}; })()".replace(
                "ARG_SEL", repr(selector))
        )
        if not box:
            raise PwTimeout(f"no element for click: {selector}")
        for t in ("mousePressed", "mouseReleased"):
            await self._command(
                "Input.dispatchMouseEvent",
                {"type": t, "x": box["x"], "y": box["y"],
                 "button": "left", "clickCount": 1},
            )

    def locator(self, selector: str) -> "BridgeLocator":
        return BridgeLocator(self, selector)

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
        raise PwTimeout(f"wait_for_selector {selector} ({state}) timed out")

    async def wait_for_function(self, js: str, timeout: int = 15_000) -> None:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            await self._gate()
            if await self._eval(js):
                return
            await asyncio.sleep(0.3)
        raise PwTimeout("wait_for_function timed out")

    async def wait_for_url(self, glob: str, timeout: int = 15_000) -> None:
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
        res = await self._command(
            "Page.captureScreenshot", {"format": "png"}) or {}
        data = res.get("data")
        if not data:
            raise BridgeEvalError("captureScreenshot returned no data")
        Path(path).write_bytes(base64.b64decode(data))

    async def content(self) -> str:
        return await self.evaluate("document.documentElement.outerHTML") or ""


class BridgeLocator:
    def __init__(self, page: BridgePage, selector: str):
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
        self.page._url = await self.page.fresh_url()

    async def click(self) -> None:
        await self.page._gate(writes=True)
        await self.page._trusted_click(self.selector)
        self.page._url = await self.page.fresh_url()

    @property
    def first(self) -> "BridgeLocator":
        return self

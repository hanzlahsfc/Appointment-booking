"""CAPTCHA solving via external solver services — plain HTTP, no SDK, so
nothing needs to be added to the bundled offline wheels.

Default provider is 2Captcha (api.2captcha.com): POST in.php creates the
task, poll res.php until CAPCHA_READY returns the token. CapSolver and
Anti-Captcha speak nearly the same protocol — swap by changing BASE and the
request shape in `_submit`/`_poll` if ever needed.

A solver token is only as good as the page it's injected into — the driver
fills #g-recaptcha-response and fires grecaptcha's success callback.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

BASE = "https://api.2captcha.com"


class CaptchaError(Exception):
    pass


async def solve_recaptcha(api_key: str, sitekey: str, page_url: str,
                          timeout_s: int = 120, poll_s: float = 5.0) -> str:
    """Return a g-recaptcha-response token, or raise CaptchaError."""
    import httpx
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{BASE}/in.php", data={
            "key": api_key, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page_url, "json": 1,
        })
        data = r.json()
        if data.get("status") != 1:
            raise CaptchaError(f"solver rejected task: {data.get('request')}")
        task_id = data["request"]

        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_s)
            r = await http.get(f"{BASE}/res.php", params={
                "key": api_key, "action": "get", "id": task_id, "json": 1})
            data = r.json()
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") != "CAPCHA_NOT_READY":
                raise CaptchaError(f"solver failed: {data.get('request')}")
    raise CaptchaError(f"solver timed out after {timeout_s}s")


async def extract_sitekey(page) -> str | None:
    """Pull the reCAPTCHA sitekey off the live page — .g-recaptcha's
    data-sitekey, the iframe ?k= param, or ___grecaptcha_cfg config."""
    try:
        return await page.evaluate(
            "() => {"
            " const el = document.querySelector('.g-recaptcha[data-sitekey],"
            "  [data-sitekey]');"
            " if (el) return el.getAttribute('data-sitekey');"
            " const f = document.querySelector("
            "  'iframe[src*=recaptcha]');"
            " if (f) { const m = f.src.match(/[?&]k=([^&]+)/);"
            "   if (m) return m[1]; }"
            " const cfg = window.___grecaptcha_cfg;"
            " if (cfg && cfg.clients) {"
            "  for (const k in cfg.clients) {"
            "   const c = cfg.clients[k];"
            "   for (const w in (c||{})) {"
            "    const o = c[w];"
            "    if (o && o.sitekey) return o.sitekey; } } }"
            " return null; }"
        )
    except Exception:
        return None


async def inject_token(page, token: str) -> bool:
    """Fill every g-recaptcha-response field and fire the success callback —
    a filled-but-uncalled token often isn't accepted by the form."""
    try:
        return bool(await page.evaluate(
            f"""() => {{
                const t = {token!r};
                let done = false;
                document.querySelectorAll(
                  'textarea[name="g-recaptcha-response"],'
                  '#g-recaptcha-response').forEach(el => {{
                    el.value = t;
                    el.innerHTML = t;
                    done = true;
                }});
                const cfg = window.___grecaptcha_cfg;
                if (cfg && cfg.clients) {{
                    for (const k in cfg.clients) {{
                        const c = cfg.clients[k];
                        for (const w in (c || {{}})) {{
                            const o = c[w];
                            if (o && o.callback) {{
                                try {{ o.callback(t); done = true; }}
                                catch(e) {{}}
                            }}
                        }}
                    }}
                }}
                return done;
            }}"""
        ))
    except Exception:
        return False


async def solve_on_page(page, api_key: str, timeout_s: int = 120) -> str | None:
    """Full solve: find sitekey on the page → solver → inject. Returns an
    error string on failure, None on success."""
    sitekey = await extract_sitekey(page)
    if not sitekey:
        return "no reCAPTCHA sitekey found on this page — solve it by hand"
    try:
        token = await solve_recaptcha(
            api_key, sitekey, getattr(page, "url", "") or "", timeout_s)
    except CaptchaError as exc:
        return str(exc)
    if not await inject_token(page, token):
        return "solver returned a token but the page had no response field"
    return None

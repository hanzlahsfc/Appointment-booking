"""Recon helper (§2.4 / §11).

Logs into the real GVCW portal headful (once, WAF-cleared), opens the profile's
booking page, and records everything the slot-picker does:

  - every JSON/XHR response it makes  -> recon/json/<ts>__<hash>.json
  - the post-login redirect + page URLs
  - the rendered DOM (screenshot + HTML)
  - a readable findings summary in recon/findings.txt

Run it, then CLICK THROUGH the slot picker for ~a minute (pick a VAC, a visa
type, a date) so the picker's API calls get captured:

    .venv/bin/python recon.py --profile "Ahmed - Work Permit" --watch 120

Findings go to ./recon/ — share that folder back.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from app.browser import IncapsulaHandler, WAFBlocked
from app.config import load_config
from app.session import LOGIN_URL, load_credentials

OUT = Path(__file__).resolve().parent / "recon"


def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def find_profile(cfg, name: str | None):
    if name:
        for p in cfg.profiles:
            if p.name == name or name.lower() in p.name.lower():
                return p
        raise SystemExit(f"no profile matching {name!r}; have: {[p.name for p in cfg.profiles]}")
    if not cfg.profiles:
        raise SystemExit("no profiles in config.yaml")
    return cfg.profiles[0]


def slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


async def wait_for_login_form(page, timeout: int = 90) -> None:
    """Wait until either a login form renders or the WAF hard-blocks."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            n = await page.locator("input").count()
            body = (await page.locator("body").inner_text(timeout=800)).strip()
        except Exception:
            n, body = 0, ""
        if n > 0:
            # distinguish login page vs WAF iframe by scanning frames
            return
        if "access denied" in body.lower():
            raise WAFBlocked("login page was replaced by an Incapsula block")
        await asyncio.sleep(2)
    raise TimeoutError("no login form and no block shell appeared")


async def main() -> int:
    ap = argparse.ArgumentParser(description="GVCW recon recorder (§2.4)")
    ap.add_argument("--profile", default=None, help="profile name (any partial match)")
    ap.add_argument("--watch", type=int, default=120, help="seconds to let you click through the picker (default 120)")
    args = ap.parse_args()

    cfg = load_config()
    profile = find_profile(cfg, args.profile)
    creds = load_credentials(cfg.creds_dir / f"{slug(profile.name)}.json")

    (OUT / "json").mkdir(parents=True, exist_ok=True)
    findings: list[str] = []
    json_hits: list[tuple[str, str, int, str]] = []

    print(f"==> recon profile: {profile.name} ({profile.category} @ {profile.location})")
    print(f"==> login URL:     {LOGIN_URL}")
    print(f"==> booking URL:   {profile.booking_url}")
    print("==> launching headful browser — leave the window open while recording\n")

    captured: list[dict] = []

    async def on_response(resp):
        url = resp.url
        ct = resp.headers.get("content-type", "")
        is_json = "json" in ct
        hit = is_json or any(k in url.lower() for k in ("api", "ajax", "slot", "appointment", "date"))
        if resp.request.resource_type in ("xhr", "fetch") and (is_json or hit):
            try:
                body = await resp.body()
            except Exception:
                return
            if body and body.strip():
                fname = f"{ts()}__{hashlib.md5(url.encode()).hexdigest()[:8]}.json"
                (OUT / "json" / fname).write_bytes(body)
                json_hits.append((f"{resp.status}", url, len(body), fname))
                captured.append({"status": resp.status, "url": url, "file": fname})
                tag = "JSON" if is_json else "REQ"
                print(f"   [{tag}] {resp.status} {url[:120]} ({len(body)}B) -> {fname}")
                if len(captured) > 200:
                    captured.pop(0)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-GB", timezone_id="Asia/Karachi")
        page = await ctx.new_page()

        # ---- 1. login ----
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        await wait_for_login_form(page)
        inc = IncapsulaHandler()
        try:
            await inc.wait_for_clear(page)
        except WAFBlocked:
            findings.append("WAF hard-block (Error 17/Access denied) during login — IP rejected pre-login.")
            print("BLOCKED during login — no further capture possible from this IP.")
            await browser.close()
            _write_findings(findings)
            return 2

        await page.wait_for_timeout(1500)
        inputs = await page.locator("input").count()
        login_desc = []
        for i in range(inputs):
            t = await page.locator("input").nth(i).get_attribute("type")
            nm = await page.locator("input").nth(i).get_attribute("name") or ""
            login_desc.append(f"input[{i}] type={t} name={nm}")
        findings.append("LOGIN FORM: " + "; ".join(login_desc) + f" (page title: {await page.title()})")
        print("login form fields:", login_desc)

        # fill + submit
        filled = False
        for i in range(inputs):
            t = (await page.locator("input").nth(i).get_attribute("type") or "")
            nm = (await page.locator("input").nth(i).get_attribute("name") or "").lower()
            if t in ("text", "email", "tel") or "user" in nm or "mail" in nm or nm in ("edit-name",):
                await page.locator("input").nth(i).fill(creds["username"])
                filled = True
            elif t == "password" or "pass" in nm:
                await page.locator("input").nth(i).fill(creds["password"])
        if not filled:
            # try known selectors
            try:
                await page.locator("#edit-name, input[name='username'], #email").first.fill(creds["username"])
                await page.locator("#edit-pass, input[name='password']").fill(creds["password"])
            except Exception:
                print("WARNING: could not locate username field — fill it manually.")
                await asyncio.sleep(15)

        await page.locator("button[type=submit], input[type=submit], .form-submit").first.click(timeout=10000)
        await page.wait_for_timeout(6000)
        post_login = page.url
        findings.append(f"POST-LOGIN URL: {post_login}")
        print(f"post-login URL: {post_login}")
        (OUT / "post-login.png").write_bytes(await page.screenshot(full_page=False))

        # ---- 2. booking page + recording ----
        page.remove_listener("response", on_response)
        page.on("response", on_response)
        await page.goto(profile.booking_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)
        try:
            await inc.wait_for_clear(page)
        except WAFBlocked:
            pass
        await page.wait_for_timeout(2000)
        findings.append(f"BOOKING PAGE URL: {page.url}")
        print(f"booking page URL: {page.url}")
        print(f"==> now click around the slot picker for up to {args.watch}s (VAC / visatype / a date)...")

        deadline = time.time() + args.watch
        while time.time() < deadline:
            await asyncio.sleep(5)

        # ---- 3. dump DOM ----
        (OUT / "booking-page.html").write_text(await page.content())
        (OUT / "booking-page.png").write_bytes(await page.screenshot(full_page=True))
        findings.append(f"booking DOM dumped: {len(await page.content())} bytes")
        print("captured final DOM + screenshot.")

        await ctx.close()
        await browser.close()

    _write_findings(findings, json_hits)
    print("\n===== DONE =====")
    print(f"findings summary -> {OUT / 'findings.txt'}")
    print(f"raw JSON/API captures -> {OUT / 'json'}/")
    print(f"post-login URL, DOM, screenshots -> {OUT}/")
    return 0


def _write_findings(findings: list[str], hits: list[tuple] | None = None) -> None:
    lines = [f"# GVCW recon ({ts()})", ""] + findings + [""]
    if hits:
        lines += ["## Captured JSON/API endpoints", ""]
        lines += [f"{s}  {len(b)}B  {u}   -> {f}" for s, u, b, f in hits]
        lines += [""]
    (OUT / "findings.txt").write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
"""Recon recorder (§2.4 / §11).

Opens the real GVCW portal in a browser YOU are signed into, then records
everything that flows through it — every request (including POST bodies),
every response — and distils it into artifacts that pin down the slot picker
and the reserve call.

Two ways to get an authenticated session:

  1. Default — a dedicated persistent profile. The browser opens on the login
     page; YOU sign in and solve the reCAPTCHA once. That profile is reused on
     later runs, so the session stays warm.
         python recon.py --profile "Ahmed - Work Permit"

  2. Attach to a Chrome you started yourself with a debug port. Note Chrome 136+
     ignores --remote-debugging-port on the DEFAULT profile, so it must be a
     dedicated profile:
         chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\chrome-recon
         python recon.py --cdp http://127.0.0.1:9222

Then click through the picker (VAC → visa type → date). If a slot is actually
available, booking it manually here is the ONLY way to capture the reserve POST,
which is why --continuous exists: leave it running and the next real booking is
captured whenever it happens.

Outputs (all under ./recon/):
    har/<ts>.har            full request/response capture
    booking-flow.json       redacted endpoint map + request field names
    findings.txt            login form, post-login URL, page URLs, notes
    post-login.png, booking-page.html, booking-page.png
"""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from app.browser import IncapsulaHandler, WAFBlocked
from app.config import load_config
from app.recorder import RecorderConfig, TrafficRecorder
from app.session import LOGIN_URL, load_credentials

OUT = Path(__file__).resolve().parent / "recon"
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent / "data" / "chrome-profile"


def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def find_profile(cfg, name: str | None):
    if name:
        for p in cfg.profiles:
            if p.name == name or name.lower() in p.name.lower():
                return p
        raise SystemExit(f"no profile matching {name!r}; have: {[p.name for p in cfg.profiles]}")
    if not cfg.profiles:
        raise SystemExit("no profiles in config.yaml")
    return cfg.profiles[0]


async def describe_login_form(page: Page) -> list[str]:
    """Record what the login form actually looks like (selectors for later)."""
    found: list[str] = []
    try:
        inputs = await page.locator("input").count()
    except Exception:
        inputs = 0
    for i in range(inputs):
        try:
            el = page.locator("input").nth(i)
            found.append(
                "input[{i}] type={t} name={n} id={i2} placeholder={p}".format(
                    i=i,
                    t=await el.get_attribute("type"),
                    n=await el.get_attribute("name"),
                    i2=await el.get_attribute("id"),
                    p=await el.get_attribute("placeholder"),
                )
            )
        except Exception:
            continue
    for selector in ("button[type=submit]", "input[type=submit]", ".form-submit", "button:has-text('Sign in')"):
        try:
            if await page.locator(selector).count():
                found.append(f"submit candidate: {selector}")
        except Exception:
            continue
    for frame in page.frames:
        if "recaptcha" in (frame.url or "").lower():
            found.append(f"recaptcha frame present: {frame.url[:120]}")
    return found


async def wait_for_human_login(page: Page, timeout: int, poll: int = 3) -> str:
    """Block until the page is definitively past the login form.

    Positive detection only. A transient reCAPTCHA error can blank the password
    field and briefly change the page, so require the login markers to stay
    absent for several consecutive checks before declaring success.
    """
    elapsed = 0
    stable = 0
    announced = False
    while elapsed < timeout:
        url = page.url or ""
        on_login = "login" in url.lower()
        try:
            has_password = await page.locator("input[type=password]").count() > 0
        except Exception:
            has_password = False
        try:
            has_captcha = any(
                "recaptcha" in (frame.url or "").lower() for frame in page.frames
            )
        except Exception:
            has_captcha = False

        if not on_login and not has_password and not has_captcha:
            stable += 1
            if stable >= 3:
                return url
        else:
            stable = 0
            if not announced or elapsed % 30 == 0:
                note = "captcha still on the page" if has_captcha else "login form present"
                print(f"    waiting for sign-in… ({elapsed}s, {note}) — {url[:90]}")
                announced = True
        await asyncio.sleep(poll)
        elapsed += poll
    raise TimeoutError(f"no sign-in detected after {timeout}s (still at {page.url})")


async def wait_for_login_form(page: Page, timeout: int = 90) -> None:
    """Wait until a login form renders, or the WAF blocks us."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            n = await page.locator("input").count()
            body = (await page.locator("body").inner_text(timeout=800)).strip()
        except Exception:
            n, body = 0, ""
        if n > 0:
            return
        if "access denied" in body.lower():
            raise WAFBlocked("login page was replaced by an Incapsula block")
        await asyncio.sleep(2)
    raise TimeoutError("no login form and no block shell appeared")


async def open_browser(pw, args) -> tuple[BrowserContext, Page]:
    """Attach to a debug-port Chrome, or launch the dedicated warm profile."""
    if args.cdp:
        print(f"==> attaching to existing Chrome at {args.cdp}")
        browser = await pw.chromium.connect_over_cdp(args.cdp)
        if not browser.contexts:
            raise SystemExit("attached, but the browser has no context/page open")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return ctx, page

    profile_dir = Path(args.profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"==> launching Chrome with profile {profile_dir} (headful)")
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=bool(getattr(args, "headless", False)),
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1366, "height": 900},
        locale="en-GB",
        timezone_id="Asia/Karachi",
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return ctx, page


async def maybe_auto_login(page: Page, creds: dict | None) -> None:
    """Optional: fill the credential fields from the creds file.

    reCAPTCHA still has to be solved by hand, so this only shortens the typing.
    """
    if not creds:
        return
    try:
        await page.locator("#edit-name, input[name='username'], input[type=email]").first.fill(
            creds["username"], timeout=8000
        )
        await page.locator("#edit-pass, input[name='password'], input[type=password]").first.fill(
            creds["password"], timeout=8000
        )
        print("    credentials filled — solve the reCAPTCHA and press Sign in")
    except Exception as exc:
        print(f"    could not auto-fill credentials ({exc}); type them manually")


async def _waf_state(page: Page) -> tuple[bool, str]:
    """True + evidence when the page is an Incapsula block rather than the site."""
    try:
        body = (await page.locator("body").inner_text(timeout=2000)).lower()
    except Exception:
        body = ""
    for marker in ("access denied", "error 17", "incapsula incident"):
        if marker in body:
            return True, marker
    for frame in page.frames:
        if "_incapsula" in (frame.url or "").lower():
            return True, "incapsula frame"
    return False, ""


async def probe(args) -> int:
    """Report whether a REAL browser gets the login form, then exit.

    This is the check that distinguishes "blocked egress" from "curl cannot run
    the JS challenge" — plain curl always fails, a browser may well pass.
    """
    print(f"==> probing {LOGIN_URL} (headless={bool(args.headless)})")
    async with async_playwright() as pw:
        ctx, page = await open_browser(pw, args)
        try:
            try:
                response = await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
                status = response.status if response is not None else None
            except Exception as exc:
                print(f"RESULT: navigation failed: {exc}")
                return 3

            await page.wait_for_timeout(4000)
            blocked, evidence = await _waf_state(page)
            title = await page.title()
            try:
                body = " ".join((await page.locator("body").inner_text(timeout=2000)).split())[:240]
            except Exception:
                body = ""
            inputs = await page.locator("input").count()
            fields = await describe_login_form(page)

            print(f"  status     : {status}")
            print(f"  final url  : {page.url}")
            print(f"  title      : {title}")
            print(f"  inputs     : {inputs}")
            for line in fields:
                print(f"    {line}")
            print(f"  body       : {body}")

            if blocked:
                print(f"RESULT: WAF BLOCKED ({evidence}) — no clearance from this egress.")
                return 2
            if inputs == 0:
                print("RESULT: page loaded but no form fields — needs a manual look.")
                return 1
            print("RESULT: OK — a real browser reaches the login form.")
            print("        Re-run without --probe and sign in to capture the flow.")
            return 0
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


async def main() -> int:
    ap = argparse.ArgumentParser(description="GVCW recon recorder (§2.4)")
    ap.add_argument("--profile", default=None, help="profile name (any partial match)")
    ap.add_argument("--cdp", default=None, help="attach to Chrome at this CDP URL, e.g. http://127.0.0.1:9222")
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR), help="persistent Chrome profile dir")
    ap.add_argument("--watch", type=int, default=0, help="seconds to record before exiting (0 = until Ctrl+C)")
    ap.add_argument("--login-timeout", type=int, default=600, help="seconds to wait for your sign-in (default 600)")
    ap.add_argument("--auto-login", action="store_true", help="pre-fill credentials from data/creds (reCAPTCHA still manual)")
    ap.add_argument("--all", action="store_true", help="record every resource type, not just document/xhr/fetch")
    ap.add_argument("--probe", action="store_true", help="reachability + login-form check only (no sign-in), then exit")
    ap.add_argument("--headless", action="store_true", help="run the probe browser headless (headful has better WAF odds)")
    args = ap.parse_args()

    cfg = load_config()
    profile = find_profile(cfg, args.profile)

    if args.probe:
        return await probe(args)

    creds = None
    if args.auto_login:
        creds = load_credentials(cfg.creds_dir / f"{slug(profile.name)}.json")

    OUT.mkdir(parents=True, exist_ok=True)
    findings: list[str] = []

    print(f"==> recon profile: {profile.name} ({profile.category} @ {profile.location})")
    print(f"==> login URL:     {LOGIN_URL}")
    print(f"==> booking URL:   {profile.booking_url}")

    recorder = TrafficRecorder(
        RecorderConfig(out_dir=OUT, capture_all=args.all)
    )

    async with async_playwright() as pw:
        ctx, page = await open_browser(pw, args)
        inc = IncapsulaHandler()

        try:
            # ---- 1. login page ----
            if "login" not in (page.url or "").lower():
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            await wait_for_login_form(page)
            try:
                await inc.wait_for_clear(page)
            except WAFBlocked as exc:
                findings.append(f"WAF block during login: {exc}")
                print(f"BLOCKED during login ({exc}).")
                print("No capture possible — see docs/OPERATIONS.md §6 (egress check).")
                _write_findings(findings)
                return 2

            login_fields = await describe_login_form(page)
            findings.append("LOGIN FORM: " + "; ".join(login_fields))
            print("login form fields:")
            for line in login_fields:
                print("   " + line)

            await maybe_auto_login(page, creds)

            print("\n==> CALL TO ACTION: sign in in the browser window now")
            print("    (solve the reCAPTCHA; the session is saved to the profile dir)\n")
            post_login = await wait_for_human_login(page, args.login_timeout)
            findings.append(f"POST-LOGIN URL: {post_login}")
            print(f"post-login URL: {post_login}")
            try:
                (OUT / "post-login.png").write_bytes(await page.screenshot(full_page=False))
            except Exception:
                pass

            # ---- 2. start recording before touching the picker ----
            recorder.attach(ctx)
            print("==> recording started (requests + responses + bodies)")

            await page.goto(profile.booking_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            findings.append(f"BOOKING PAGE URL: {page.url}")
            print(f"booking page URL: {page.url}")

            if args.watch:
                print(f"==> click through the picker for up to {args.watch}s "
                      f"(VAC → visa type → date; book manually if a slot is open)")
                deadline = time.time() + args.watch
                while time.time() < deadline:
                    await asyncio.sleep(10)
                    # flush as we go: an interrupted run should still leave a capture
                    recorder.write_har()
            else:
                print("==> click through the picker now. Recording until Ctrl+C.")
                print("    If a slot opens, book it manually — that captures the reserve call.")
                try:
                    while True:
                        await asyncio.sleep(10)
                        recorder.write_har()  # periodic flush so nothing is lost
                except (KeyboardInterrupt, asyncio.CancelledError):
                    print("\n==> stopping recording")

            # ---- 3. dump DOM + close out ----
            try:
                html = await page.content()
                (OUT / "booking-page.html").write_text(html)
                (OUT / "booking-page.png").write_bytes(await page.screenshot(full_page=True))
                findings.append(f"booking DOM dumped: {len(html)} bytes")
                print("captured final DOM + screenshot")
            except Exception as exc:
                findings.append(f"DOM dump failed: {exc}")

            await recorder.aclose()
            har_path = recorder.write_har()
            flow_path = recorder.write_flow()
            findings.append(f"recorded entries: {len(recorder.entries)} (errors: {recorder.errors})")
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    _write_findings(findings)
    print("\n===== DONE =====")
    print(f"findings      -> {OUT / 'findings.txt'}")
    print(f"HAR           -> {OUT / 'har'}")
    print(f"endpoint map  -> {OUT / 'booking-flow.json'}")
    print("Share recon/findings.txt + booking-flow.json (both are redacted).")
    return 0


def _write_findings(findings: list[str]) -> None:
    lines = [f"# GVCW recon ({ts()})", ""] + findings + [""]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "findings.txt").write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

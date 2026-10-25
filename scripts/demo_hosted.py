"""Local hosted-mode demonstration driver.

Simulates a remote client over the real HTTP + WebSocket surface — no access
to server internals, so a pass here is the milestone-8 demo: manual portal
login, monitoring, assisted reservation, manual completion.

    # terminal 1 — host side (mock portal is a test-only allowance):
    set AB_ALLOW_MOCK_PORTAL=1
    python -m app.hosted serve --with-mock-portal --booking-mode assist --no-booking-dry-run

    # terminal 2 — provision + drive:
    python -m app.hosted add-client demo --preset mock      # prints a one-time access code
    python scripts/demo_hosted.py --code ab-... --seed "2026-12-01 09:00"

Keyboard-only input is used (Tab/text/Enter) — no coordinate guessing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect as ws_connect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.config import load_config  # noqa: E402

USERNAME_FIELD_PLACEHOLDER = True


async def _send(ws, obj):
    await ws.send(json.dumps(obj))


async def _wait_msg(ws, pred, timeout, what):
    """Read frames until pred matches; frames keep flowing meanwhile."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if pred(msg):
            return msg
    raise TimeoutError(f"timed out waiting for {what}")


async def _run(args) -> int:
    cfg = load_config()
    base = args.base.rstrip("/")
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://")

    async with httpx.AsyncClient(timeout=15) as http:
        print(f"[1] exchanging access code at {base}")
        resp = await http.post(f"{base}/api/login", json={"code": args.code})
        if resp.status_code != 200:
            print(f"    login failed: {resp.status_code} {resp.text}")
            return 1
        token = resp.json()["token"]
        print("    ok — session issued")

        async with ws_connect(f"{ws_url}/ws/screen?token={token}") as ws:
            hello = await _wait_msg(ws, lambda m: m.get("type") == "hello", 10, "hello")
            print(f"[2] stream up; client={hello['client']['username']} "
                  f"profile={hello['client']['profile_name']}")

            # wait for the monitor to report the login page, then take over
            print("[3] waiting for monitor to reach the portal login page…")
            await _wait_msg(
                ws,
                lambda m: m.get("type") == "monitor"
                and m["state"].get("status") in ("awaiting_login", "ok"),
                60, "awaiting_login/ok",
            )

            print("[4] taking control and signing in to the portal by keyboard")
            await _send(ws, {"type": "control", "action": "take"})
            await _wait_msg(
                ws, lambda m: m.get("type") == "status" and m.get("control") == "manual",
                10, "manual control",
            )
            # go to the portal page, then Tab into the first field
            await _send(ws, {"type": "navigate", "target": "home"})
            await asyncio.sleep(1.5)
            for step in (
                {"type": "key", "event": "press", "key": "Tab"},
                {"type": "text", "text": args.portal_username},
                {"type": "key", "event": "press", "key": "Tab"},
                {"type": "text", "text": args.portal_password},
                {"type": "key", "event": "press", "key": "Enter"},
            ):
                await _send(ws, step)
                await asyncio.sleep(0.5)
            await _wait_msg(
                ws,
                lambda m: m.get("type") == "status" and "/en/account" in m.get("url", ""),
                20, "post-login redirect",
            )
            print("    portal login accepted — releasing control")
            await _send(ws, {"type": "control", "action": "release"})

            print("[5] monitoring resumed; waiting for a quiet poll")
            await _wait_msg(
                ws,
                lambda m: m.get("type") == "monitor" and m["state"].get("status") == "ok",
                60, "monitor ok",
            )
            print("    monitor polling cleanly")

            if args.seed:
                date_str, time_str = args.seed.split()
                resp = await http.post(
                    f"{cfg.hosted.mock_portal_url}/__admin__/slots",
                    json={"date": date_str, "time": time_str},
                )
                print(f"[6] seeded slot {date_str} {time_str} "
                      f"({resp.status_code}) — waiting for assist claim")
            else:
                print("[6] waiting for a slot to appear on the portal…")

            await _wait_msg(
                ws,
                lambda m: m.get("type") == "monitor"
                and m["state"].get("status") in ("needs_human", "holding"),
                90, "needs_human",
            )
            print("    SLOT CLAIMED — the agent reserved it and is holding")

            print("[7] taking control to complete the applicant form")
            await _send(ws, {"type": "control", "action": "take"})
            await _wait_msg(
                ws, lambda m: m.get("type") == "status" and m.get("control") == "manual",
                10, "manual control",
            )
            await asyncio.sleep(1.0)
            for step in (
                {"type": "key", "event": "press", "key": "Tab"},
                {"type": "text", "text": args.passport},
                {"type": "key", "event": "press", "key": "Tab"},
                {"type": "text", "text": args.fullname},
                {"type": "key", "event": "press", "key": "Tab"},
                {"type": "key", "event": "press", "key": "Enter"},
            ):
                await _send(ws, step)
                await asyncio.sleep(0.5)
            await asyncio.sleep(2.0)

            status = (await http.get(f"{base}/api/status")).json()
            print(f"    browser now at: {(status.get('browser') or {}).get('url')}")

            state = await http.get(f"{cfg.hosted.mock_portal_url}/__admin__/state")
            bookings = state.json().get("bookings", [])
            if bookings:
                print(f"[8] CONFIRMED on the portal: {bookings[-1]}")
            else:
                print("[8] portal shows no confirmed booking — check the form fill")
                return 1

            # tell the backend we finished — it verifies the marker, then
            # records the held attempt as booked
            resp = await http.post(f"{base}/api/booking/complete")
            comp = resp.json()
            print(f"    completion check: {comp}")
            if not comp.get("booked"):
                print("    marker not seen — releasing control so the monitor "
                      "verifies on its next check")
                await _send(ws, {"type": "control", "action": "release"})
                await _wait_msg(
                    ws,
                    lambda m: m.get("type") == "monitor"
                    and m["state"].get("status") == "booked",
                    30, "booked status",
                )
                print("    monitor recorded the booking as booked")

            records = (await http.get(f"{base}/api/records")).json()
            booked = [b for b in records["bookings"] if b["status"] == "booked"]
            print(f"    booking_log: {len(records['bookings'])} attempts "
                  f"({len(booked)} booked — profile cannot book again)")
            print("\nDEMO COMPLETE — manual login -> monitoring -> assisted "
                  "reservation -> manual completion -> recorded as booked")
            return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://127.0.0.1:8081")
    p.add_argument("--code", required=True, help="the client's access code")
    p.add_argument("--seed", default=None,
                   help='"YYYY-MM-DD HH:MM" — seed a mock slot at this point')
    p.add_argument("--portal-username", default="recon@example.com")
    p.add_argument("--portal-password", default="s3cret")
    p.add_argument("--passport", default="AB1234567")
    p.add_argument("--fullname", default="Demo Applicant")
    args = p.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

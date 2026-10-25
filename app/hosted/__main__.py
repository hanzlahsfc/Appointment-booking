"""Hosted-client operator CLI.

    python -m app.hosted serve [--port 8081] [--with-mock-portal]
    python -m app.hosted add-client USERNAME --preset mock --password ...
    python -m app.hosted add-client USERNAME --booking-url ... --origin ... [opts]
    python -m app.hosted list-clients
    python -m app.hosted passwd USERNAME --password ...
    python -m app.hosted deactivate USERNAME | activate USERNAME
    python -m app.hosted seed-mock --date 2026-12-01 --time 09:00

Account provisioning is deliberately CLI-only: there is no signup endpoint a
remote user could reach.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.hosted.store import HostedStore  # noqa: E402

logger = logging.getLogger("hosted")


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# ── client provisioning ──────────────────────────────────────────────────


def _cmd_add_client(args) -> int:
    cfg = load_config()
    store = HostedStore(cfg.hosted.db_path, cfg.db_path)
    try:
        if args.preset == "mock":
            if not (cfg.hosted.allow_mock_portal or _env_flag("AB_ALLOW_MOCK_PORTAL")):
                print(
                    "refusing to provision a mock-portal client: set "
                    "hosted.allow_mock_portal: true in config.yaml (test-only)",
                    file=sys.stderr,
                )
                return 2
            from app.hosted.presets import mock_preset

            fields = mock_preset(cfg.hosted.mock_portal_url, args.username)
        else:
            for required in ("booking_url", "origin", "category", "location"):
                if not getattr(args, required.replace("-", "_"), None):
                    print(
                        f"--{required.replace('_','-')} is required without --preset",
                        file=sys.stderr,
                    )
                    return 2
            selectors = {}
            if args.selectors_file:
                import json

                selectors = json.loads(Path(args.selectors_file).read_text())
            fields = {
                "profile_name": args.profile_name or f"client:{args.username}",
                "booking_url": args.booking_url,
                "category": args.category,
                "location": args.location,
                "allowed_origins": args.origin,
                "selectors": selectors,
                "auto_book": bool(args.auto_book),
            }
        client = store.add_client(
            username=args.username,
            display_name=args.name or args.username,
            password=args.password,
            **fields,
        )
        print(
            f"client #{client['id']} created: {client['username']} "
            f"({client['display_name']}) profile={client['profile_name']} "
            f"origins={client['allowed_origins']}"
        )
        return 0
    except Exception as exc:
        print(f"add-client failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _cmd_list_clients(_args) -> int:
    cfg = load_config()
    store = HostedStore(cfg.hosted.db_path, cfg.db_path)
    try:
        clients = store.list_clients()
        if not clients:
            print("no clients provisioned")
            return 0
        for c in clients:
            flag = "" if c["active"] else "  [disabled]"
            print(
                f"#{c['id']:>3} {c['username']:<20} {c['display_name']:<24} "
                f"profile={c['profile_name']} auto_book={c['auto_book']}{flag}"
            )
        return 0
    finally:
        store.close()


def _cmd_passwd(args) -> int:
    cfg = load_config()
    store = HostedStore(cfg.hosted.db_path, cfg.db_path)
    try:
        client = store.client_by_username(args.username)
        if client is None:
            print(f"no such client: {args.username}", file=sys.stderr)
            return 1
        store.set_password(client["id"], args.password)
        print(f"password updated for {client['username']} (sessions revoked)")
        return 0
    finally:
        store.close()


def _cmd_set_active(args, active: bool) -> int:
    cfg = load_config()
    store = HostedStore(cfg.hosted.db_path, cfg.db_path)
    try:
        client = store.client_by_username(args.username)
        if client is None:
            print(f"no such client: {args.username}", file=sys.stderr)
            return 1
        store.set_active(client["id"], active)
        print(f"{client['username']} {'enabled' if active else 'disabled'}")
        return 0
    finally:
        store.close()


def _cmd_seed_mock(args) -> int:
    import httpx

    cfg = load_config()
    url = f"{cfg.hosted.mock_portal_url}/__admin__/slots"
    try:
        resp = httpx.post(
            url,
            json={
                "date": args.date,
                "time": args.time,
                "vac": args.vac,
                "category": args.category,
            },
            timeout=5,
        )
    except Exception as exc:
        print(f"could not reach mock portal at {url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print(f"mock portal refused: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1
    print(f"seeded slot {args.date} {args.time} @ {args.vac} ({args.category})")
    return 0


# ── serve ────────────────────────────────────────────────────────────────


async def _serve(args) -> int:
    import uvicorn

    from app.store import Store
    from app.hosted.manager import BrowserHostManager
    from app.hosted.monitor import HostedMonitor
    from app.hosted.server import HostedRuntime, create_app

    cfg = load_config()
    if _env_flag("AB_ALLOW_MOCK_PORTAL"):
        cfg.hosted.allow_mock_portal = True
    if args.booking_mode:
        cfg.booking.mode = args.booking_mode
    if args.booking_dry_run is not None:
        cfg.booking.dry_run = args.booking_dry_run

    hstore = HostedStore(cfg.hosted.db_path, cfg.db_path)
    store = Store(cfg.db_path)
    manager = BrowserHostManager(cfg.hosted)
    runtime = HostedRuntime(cfg, hstore, store, manager)
    app = create_app(runtime)

    hstore.purge_expired_sessions()
    await manager.start()

    # A monitor per active client. Each client's profile is also registered
    # into cfg.profiles so BookingAgent's per-profile opt-in guard
    # (booking_enabled_for) applies unchanged — hosted defaults stay safe:
    # booking is off unless the operator enables a mode AND the client row
    # opted in.
    monitors: list[HostedMonitor] = []
    from app.hosted.monitor import register_client_profiles

    clients = [c for c in hstore.list_clients() if c["active"]]
    register_client_profiles(cfg, clients)
    for client in clients:
        monitor = HostedMonitor(cfg, client, store, manager)
        monitor.start()
        monitors.append(monitor)
        runtime.monitors[client["id"]] = monitor

    server = uvicorn.Server(
        uvicorn.Config(
            app, host=args.host or cfg.hosted.bind, port=args.port or cfg.hosted.port,
            log_level="warning", access_log=False,
        )
    )
    asyncio.get_running_loop().create_task(server.serve())

    mock_server = None
    if args.with_mock_portal:
        if not cfg.hosted.allow_mock_portal:
            logger.error(
                "--with-mock-portal requires hosted.allow_mock_portal "
                "(or AB_ALLOW_MOCK_PORTAL=1) — it is a test-only allowance"
            )
        else:
            mock_server = _start_mock_portal(cfg)
            if mock_server is not None:
                logger.info("mock portal at %s (TEST ONLY)", cfg.hosted.mock_portal_url)

    host, port = args.host or cfg.hosted.bind, args.port or cfg.hosted.port
    logger.info("hosted dashboard http://%s:%d | clients=%d", host, port, len(monitors))

    stop = asyncio.Event()

    def _signal(*_):
        logger.info("shutdown requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()

    server.should_exit = True
    if mock_server is not None:
        mock_server.should_exit = True
    for monitor in monitors:
        await monitor.stop()
    await manager.stop_all()
    store.close()
    hstore.close()
    return 0


def _start_mock_portal(cfg):
    """Run tests/fake_portal in-process on the configured mock URL (TEST ONLY)."""
    import uvicorn

    sys.path.insert(0, str(ROOT / "tests"))
    from fake_portal.app import PortalState, create_app  # noqa: E402

    from urllib.parse import urlsplit

    port = urlsplit(cfg.hosted.mock_portal_url).port or 8099
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(PortalState()), host="127.0.0.1", port=port,
            log_level="warning", access_log=False,
        )
    )
    asyncio.get_running_loop().create_task(server.serve())
    return server


# ── arg parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.hosted")
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the hosted client server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--booking-mode", choices=["off", "assist"], default=None,
                       help="override booking mode for this run (auto is never "
                            "allowed for hosted clients)")
    serve.add_argument(
        "--booking-dry-run", action=argparse.BooleanOptionalAction, default=None,
        help="walk the booking flow without clicking reserve (default: config)",
    )
    serve.add_argument("--with-mock-portal", action="store_true",
                       help="also run the fake portal (TEST ONLY — needs "
                            "hosted.allow_mock_portal)")

    add = sub.add_parser("add-client", help="provision a client account")
    add.add_argument("username")
    add.add_argument("--name", default=None, help="display name")
    add.add_argument("--password", required=True)
    add.add_argument("--preset", choices=["mock"], default=None)
    add.add_argument("--profile-name", default=None)
    add.add_argument("--booking-url", default=None)
    add.add_argument("--origin", action="append", default=None,
                     help="allowed origin (repeatable), e.g. https://portal.example")
    add.add_argument("--category", default=None)
    add.add_argument("--location", default=None)
    add.add_argument("--selectors-file", default=None)
    add.add_argument("--auto-book", action="store_true")

    sub.add_parser("list-clients")

    pw = sub.add_parser("passwd", help="reset a client's password")
    pw.add_argument("username")
    pw.add_argument("--password", required=True)

    for name in ("activate", "deactivate"):
        sp = sub.add_parser(name)
        sp.add_argument("username")

    seed = sub.add_parser("seed-mock", help="add a slot to the running mock portal")
    seed.add_argument("--date", required=True)
    seed.add_argument("--time", required=True)
    seed.add_argument("--vac", default="lahore")
    seed.add_argument("--category", default="national_d_employment")
    return p


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    args = _build_parser().parse_args()
    if args.cmd == "serve":
        try:
            return asyncio.run(_serve(args))
        except KeyboardInterrupt:
            return 0
    if args.cmd == "add-client":
        return _cmd_add_client(args)
    if args.cmd == "list-clients":
        return _cmd_list_clients(args)
    if args.cmd == "passwd":
        return _cmd_passwd(args)
    if args.cmd == "activate":
        return _cmd_set_active(args, True)
    if args.cmd == "deactivate":
        return _cmd_set_active(args, False)
    if args.cmd == "seed-mock":
        return _cmd_seed_mock(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())

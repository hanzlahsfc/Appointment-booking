"""Entry point: load config, start scheduler, launch dashboard.

Usage:
    python main.py              # production crawl
    python main.py --demo       # demo mode — fake slots, populated dashboard
    python main.py --port 9090  # custom dashboard port (default 8080)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from app.config import load_config
from app.preflight import check_reachability
from app.session import LOGIN_URL
from app.crawler import CrawlerWorker
from app.dashboard import DashboardState
from app.notifier import Notifier
from app.store import Store
from app.web import app as fastapi_app, bind as bind_dashboard

logger = logging.getLogger("gvcw")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root.addHandler(handler)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GVCW appointment slot monitor")
    p.add_argument("--demo", action="store_true", help="Run with fabricated slots for dashboard demo")
    p.add_argument("--port", type=int, default=8080, help="Dashboard port (default: 8080)")
    p.add_argument("--host", default="127.0.0.1", help="Dashboard bind address (default: 127.0.0.1)")
    p.add_argument("--no-book", action="store_true", help="Disable auto-booking even if config enables it")
    p.add_argument(
        "--booking-mode",
        choices=["off", "assist", "auto"],
        default=None,
        help="Override app.booking.mode for this run",
    )
    p.add_argument("--skip-preflight", action="store_true", help="Skip the egress reachability check")
    return p.parse_args()


async def _preflight(cfg) -> None:
    """Refuse to run booking behind a blocked egress.

    Without this, a WAF-blocked IP looks like a healthy process that simply
    never finds slots — the worst possible failure mode for this product.
    """
    result = await check_reachability(LOGIN_URL)
    if result.ok:
        logger.info("preflight ok: %s", result.detail)
        return
    if result.blocked:
        logger.error("preflight: %s", result.detail)
        if cfg.booking.enabled:
            logger.error(
                "refusing to start auto-booking from a WAF-blocked egress; "
                "fix the network (docs/OPERATIONS.md §6) or pass --skip-preflight"
            )
            raise SystemExit(2)
    else:
        logger.warning("preflight: %s", result.detail)


def _profiles_as_dicts(cfg):
    for p in cfg.profiles:
        yield {
            "name": p.name,
            "email": p.email,
            "category": p.category,
            "location": p.location,
            "booking_url": p.booking_url,
            "auto_book": p.auto_book,
            "applicant_file": p.applicant_file,
            "prefer": p.prefer,
        }


async def main() -> int:
    setup_logging()
    args = _parse_args()
    cfg = load_config()
    if args.no_book:
        cfg.booking.mode = "off"
    elif args.booking_mode:
        cfg.booking.mode = args.booking_mode

    logger.info(
        "starting gvcw-crawler | profiles=%d | demo=%s | dry_run=%s | interval=%ds | booking=%s",
        len(cfg.profiles),
        args.demo,
        cfg.app.dry_run,
        cfg.app.poll_interval_seconds,
        cfg.booking.mode,
    )

    if not args.demo and not args.skip_preflight:
        await _preflight(cfg)

    os_environ_key = cfg.notifications.resend_api_key_env
    api_key = os.environ.get(os_environ_key, "")
    dry_run = cfg.app.dry_run or args.demo

    if not api_key and not dry_run:
        logger.error("missing env %s (Resend API key)", os_environ_key)
        return 1

    store = Store(cfg.db_path)
    notifier = Notifier(api_key=api_key, from_addr=cfg.notifications.from_addr, to=cfg.notifications.to, dry_run=dry_run)

    dashboard = DashboardState()
    bind_dashboard(dashboard, {"poll": cfg.app.poll_interval_seconds, "demo": args.demo, "dry_run": dry_run})

    server = _start_server(args.host, args.port)
    try:
        await _run_workers(cfg, store, notifier, dashboard, demo=args.demo)
    finally:
        store.close()
        await notifier.aclose()
        server.should_exit = True
    return 0


def _start_server(host: str, port: int):
    """Start uvicorn in the same asyncio loop."""
    import uvicorn
    config = uvicorn.Config(
        app=fastapi_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    asyncio.get_running_loop().create_task(server.serve())
    logger.info("dashboard http://%s:%d", host, port)
    return server


async def _run_workers(cfg, store, notifier, dashboard, demo: bool = False) -> None:
    profiles = list(_profiles_as_dicts(cfg))
    if not profiles:
        logger.warning("no profiles configured; sleeping")
        await _sleep_forever()
        return

    sem = asyncio.Semaphore(cfg.app.concurrency)
    workers = [CrawlerWorker(cfg, p, store, notifier, dashboard=dashboard, demo=demo) for p in profiles]

    async def _guard(w):
        async with sem:
            if not w.demo:
                await w.start()
            try:
                await w.run()
            finally:
                if not w.demo:
                    await w.stop()

    tasks = [asyncio.create_task(_guard(w)) for w in workers]

    stop = asyncio.Event()

    def _signal_handler(*_):
        logger.info("signal received; shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        if t.done():
            exc = t.exception()
            if exc:
                logger.error("worker task ended with error: %s", exc)
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _sleep_forever() -> None:
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
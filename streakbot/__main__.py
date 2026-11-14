"""Entry point.

    python -m streakbot                  run the panel and the scheduler
    python -m streakbot hash-password    read a password on stdin, print its hash for PANEL_PASSWORD_HASH
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import UTC, datetime

from .auth import LoginThrottle, SessionSigner, hash_password, load_or_create_session_key
from .config import ConfigError, Settings, load_settings
from .crypto import CookieBox, load_or_create_key
from .db import Repo
from .service import PanelService
from .tiktok.client import TikTokClient
from .web.app import create_app
from .web.events import EventHub

log = logging.getLogger("streakbot")
TICK_SECONDS = 60


async def tick_loop(service: PanelService, stop: asyncio.Event, every: int = TICK_SECONDS) -> None:
    """Run the scheduler every `every` seconds (aligned to the clock) until `stop` is set."""
    while not stop.is_set():
        try:
            await service.tick(datetime.now(UTC))
        except Exception:
            log.exception("tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=every - datetime.now(UTC).second % every)
        except TimeoutError:
            pass


async def run(settings: Settings) -> None:
    import uvicorn

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    hub = EventHub()
    tiktok = TikTokClient()
    service = PanelService(
        Repo(settings.data_dir / "streakbot.sqlite"),
        CookieBox(load_or_create_key(settings.data_dir / "secret.key")),
        tiktok,
        settings.default_timezone,
        on_change=hub.publish,
    )
    signer = SessionSigner(load_or_create_session_key(settings.data_dir / "session.key"), settings.password_hash)
    app = create_app(service, hub, signer, LoginThrottle(), settings.password_hash)

    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_level="warning",
        timeout_graceful_shutdown=3, access_log=False,
    ))
    server.install_signal_handlers = lambda: None  # we handle signals ourselves

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    web = asyncio.create_task(server.serve())
    ticker = asyncio.create_task(tick_loop(service, stop))
    log.info("Panel listening on port %s; scheduler running every %ss", settings.port, TICK_SECONDS)
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([stopper, web, ticker], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper and (error := task.exception()):
                raise error
    finally:
        log.info("Shutting down")
        stop.set()
        server.should_exit = True
        ticker.cancel()
        await asyncio.gather(web, ticker, return_exceptions=True)
        await tiktok.stop()


def _hash_password_command() -> int:
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("The password can't be empty.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["hash-password"]:
        sys.exit(_hash_password_command())
    if args:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        settings = load_settings(os.environ)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()

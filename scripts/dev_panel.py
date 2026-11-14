"""Run the panel locally against a fake TikTok, for UI work. Nothing is sent anywhere.

    python scripts/dev_panel.py            # http://127.0.0.1:8080, password: dev
"""
from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

import uvicorn

from streakbot.auth import LoginThrottle, SessionSigner, hash_password
from streakbot.crypto import CookieBox, load_or_create_key
from streakbot.db import Repo
from streakbot.service import PanelService
from streakbot.tiktok.client import Profile, SendResult
from streakbot.tiktok.cookies import find_cookie
from streakbot.__main__ import tick_loop
from streakbot.web.app import create_app
from streakbot.web.events import EventHub

ACCOUNTS = ["owner.acct", "rivr.x", "lee_77"]
FRIENDS = [Profile("sam.r_07", "Sam", None), Profile("jordan_9", "Jordan", None),
           Profile("kai.m", "Kai", None), Profile("mia.z", "Mia", None), Profile("sam", "sam", None)]


class StubTikTok:
    """Pretends to be TikTok. A sessionid starting with 'bad' is rejected, 'region' needs a region cookie;
    usernames containing 'no' don't exist."""

    async def whoami(self, cookies):
        await asyncio.sleep(0.6)
        sid = find_cookie(cookies, "sessionid") or ""
        if sid.startswith("bad"):
            return None
        return ACCOUNTS[sum(map(ord, sid)) % len(ACCOUNTS)]

    async def detect_region(self, sessionid):
        await asyncio.sleep(0.4)
        return None if sessionid.startswith("region") else "useast5"

    async def list_recent_chats(self, cookies, limit=20):
        await asyncio.sleep(2)
        return FRIENDS[:limit]

    async def lookup_profile(self, cookies, username):
        await asyncio.sleep(0.8)
        known = {p.username: p for p in FRIENDS}
        return known.get(username) or (None if "no" in username else Profile(username, username.title(), None))

    async def send_messages(self, cookies, usernames, message):
        results = []
        for u in usernames:
            await asyncio.sleep(1.5)
            results.append(SendResult(u, "no" not in u, None if "no" not in u else "not_found"))
        return results, list(cookies)


async def main(port: int) -> None:
    data = Path(tempfile.mkdtemp(prefix="streakpanel-"))
    hub = EventHub()
    service = PanelService(Repo(data / "db.sqlite"), CookieBox(load_or_create_key(data / "k")),
                           StubTikTok(), "Europe/Moscow", on_change=hub.publish)
    pw = hash_password("dev")
    app = create_app(service, hub, SessionSigner(b"d" * 32, pw), LoginThrottle(), pw)

    ticker = asyncio.create_task(tick_loop(service, asyncio.Event(), every=20))
    print(f"Panel: http://127.0.0.1:{port}  password: dev  data: {data}")
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                        timeout_graceful_shutdown=2)).serve()
    ticker.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    asyncio.run(main(parser.parse_args().port))

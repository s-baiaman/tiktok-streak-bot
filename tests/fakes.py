"""Test double for the TikTok client."""
from __future__ import annotations

from streakbot.tiktok.client import Profile, SendResult
from streakbot.tiktok.cookies import find_cookie
from streakbot.tiktok.errors import SessionExpired


class FakeTikTok:
    def __init__(self):
        self.whoami_result: str | None = "owner.acct"
        self.region: str | None = "useast5"
        self.outcomes: dict[str, str] = {}  # username -> error code
        self.expire: bool = False
        self.send_raises: Exception | None = None
        self.refreshed: list[dict] | None = None
        self.chats: list[Profile] = []
        self.profiles: dict[str, Profile] = {}
        self.calls: list[tuple] = []
        self.on_send = None  # optional async callback run in the middle of send_messages
        self.whoami_raises: Exception | None = None
        self.on_whoami = None  # optional async callback run inside whoami

    async def whoami(self, cookies):
        self.calls.append(("whoami", find_cookie(cookies, "sessionid")))
        if self.on_whoami is not None:
            callback, self.on_whoami = self.on_whoami, None
            await callback()
        if self.whoami_raises:
            raise self.whoami_raises
        return self.whoami_result

    async def detect_region(self, sessionid):
        self.calls.append(("detect_region",))
        return self.region

    async def inbox_works(self, cookies):
        return True

    async def list_recent_chats(self, cookies, limit=20):
        return self.chats[:limit]

    async def lookup_profile(self, cookies, username):
        return self.profiles.get(username)

    async def send_messages(self, cookies, usernames, message):
        self.calls.append(("send", tuple(usernames), message, find_cookie(cookies, "sessionid")))
        if self.on_send is not None:
            await self.on_send()
        if self.send_raises:
            raise self.send_raises
        if self.expire:
            raise SessionExpired("TikTok login expired.")
        results = [
            SendResult(u, u not in self.outcomes, self.outcomes.get(u)) for u in usernames
        ]
        return results, (self.refreshed if self.refreshed is not None else list(cookies))

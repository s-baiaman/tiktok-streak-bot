"""Playwright (Firefox, headless) client for TikTok's web app.

Privacy: this module never logs cookie values or recipient usernames.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .cookies import REGION_CODES, build_cookies, filter_tiktok_cookies
from .errors import SessionExpired, TikTokError, UiChanged

log = logging.getLogger(__name__)

BASE = "https://www.tiktok.com"
ACCOUNT_INFO_JS = """async () => {
    const r = await fetch('/passport/web/account/info/?aid=1988', {credentials: 'include'});
    let json = null;
    try { json = await r.json(); } catch (e) {}
    return {status: r.status, json: json};
}"""

# TikTok's DOM is unversioned; if sends start failing with ui_changed, update these.
MESSAGE_BUTTON = "[data-e2e='message-button']"
CHAT_INPUT = "[data-e2e='message-input-area'] [contenteditable]"
SEND_BUTTON = "[data-e2e='dm-new-send-btn']"
SKELETON = "[class*='Skeleton'], [class*='skeleton']"
CONVERSATION_ITEM = "[data-e2e='dm-new-conversation-item']"
CONVERSATION_NICKNAME = "[data-e2e='dm-new-conversation-nickname']"
PROFILE_TITLE = "[data-e2e='user-title']"
PROFILE_SUBTITLE = "[data-e2e='user-subtitle']"
PROFILE_AVATAR = "[data-e2e='user-avatar'] img"
IM_SDK_FAILURE = "initSdkCore failed"
IM_PROFILE_API = "/tiktok/v1/im/user/profile/"


@dataclass
class Profile:
    username: str
    display_name: str
    avatar_url: str | None


@dataclass
class SendResult:
    username: str
    ok: bool
    error: str | None  # "not_found" | "ui_changed" | "tiktok_error"


def interpret_account_info(result: dict | None) -> str | None:
    """Username if logged in, None if TikTok clearly says logged out; TikTokError if we can't tell."""
    if not result or result.get("json") is None or result.get("status", 0) >= 400:
        raise TikTokError("TikTok's account check didn't answer properly.")
    data = result["json"]
    if data.get("message") == "success" and (data.get("data") or {}).get("username"):
        return data["data"]["username"]
    return None


def profile_from_page(requested: str, title: str, subtitle: str, avatar_url: str | None) -> Profile:
    """Never trust the page for *who* this is: the saved username is always the one requested."""
    if subtitle.strip().lstrip("@").lower() != requested:
        raise UiChanged("The profile page layout changed.")
    return Profile(requested, title.strip() or requested, avatar_url)


def _profile_from_im(raw: dict) -> tuple[Profile, str | None] | None:
    p = raw.get("im_user_profile") or {}
    username = p.get("unique_id")
    if not username:
        return None
    avatar = (p.get("avatars") or {}).get("avatar_medium") or {}
    urls = avatar.get("url_list") or []
    return Profile(username, p.get("nick_name") or username, urls[0] if urls else None), avatar.get("uri")


def match_chat_rows(rows: list[dict], im_profiles: list[tuple[Profile, str | None]], limit: int) -> list[Profile]:
    """Map inbox rows ({'nick', 'src'}) to profiles: avatar URI first, then display name.

    Display names are not unique (two different accounts can both be called 'sam'),
    so a name collision yields every matching account; the UI always shows the real @username.
    """
    result: list[Profile] = []
    seen: set[str] = set()
    for row in rows:
        src, nick = row.get("src") or "", row.get("nick") or ""
        matches = [p for p, uri in im_profiles if uri and uri in src]
        if not matches:
            matches = [p for p, _ in im_profiles if nick and p.display_name == nick]
        for p in matches:
            if p.username not in seen:
                seen.add(p.username)
                result.append(p)
        if len(result) >= limit:
            break
    return result[:limit]


class TikTokClient:
    def __init__(self, max_browsers: int = 1, headless: bool = True):
        self._sem = asyncio.Semaphore(max_browsers)
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.firefox.launch(headless=self._headless)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    @asynccontextmanager
    async def _session(self, cookies: list[dict]) -> AsyncIterator[BrowserContext]:
        async with self._sem:
            try:
                await self.start()
                ctx = await self._browser.new_context()
            except PlaywrightError as e:
                raise TikTokError("The browser couldn't start.") from e
            try:
                await ctx.add_cookies(cookies)
                yield ctx
            except PlaywrightError as e:
                raise TikTokError("TikTok didn't load.") from e
            finally:
                try:
                    await ctx.close()
                except PlaywrightError:
                    pass

    @staticmethod
    async def _username_in(page: Page) -> str | None:
        """Logged-in username, None if logged out. Raises TikTokError if TikTok can't be reached."""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if attempt or not page.url.startswith(BASE):
                    await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
                return interpret_account_info(await page.evaluate(ACCOUNT_INFO_JS))
            except (PlaywrightError, TikTokError) as e:
                last_error = e
                await asyncio.sleep(3)
        raise TikTokError("Couldn't check the TikTok login.") from last_error

    async def whoami(self, cookies: list[dict]) -> str | None:
        async with self._session(cookies) as ctx:
            page = await ctx.new_page()
            return await self._username_in(page)

    async def inbox_works(self, cookies: list[dict]) -> bool:
        async with self._session(cookies) as ctx:
            return await self._inbox_loads(await ctx.new_page())

    @staticmethod
    async def _inbox_loads(page: Page, timeout_s: int = 30) -> bool:
        failed = asyncio.Event()
        page.on("console", lambda m: failed.set() if IM_SDK_FAILURE in m.text else None)
        await page.goto(BASE + "/messages", wait_until="domcontentloaded", timeout=60000)
        for _ in range(timeout_s):
            await page.wait_for_timeout(1000)
            if failed.is_set():
                return False
            if await page.locator(SKELETON).count() == 0:
                return True
        return False

    async def detect_region(self, sessionid: str) -> str | None:
        for code in REGION_CODES:
            try:
                if await self.inbox_works(build_cookies(sessionid, code)):
                    return code
            except TikTokError:
                log.info("region %s check failed to load", code)
        return None

    async def list_recent_chats(self, cookies: list[dict], limit: int = 20) -> list[Profile]:
        async with self._session(cookies) as ctx:
            page = await ctx.new_page()
            im_profiles: list[tuple[Profile, str | None]] = []

            async def on_response(response):
                if IM_PROFILE_API in response.url:
                    try:
                        data = await response.json()
                    except Exception:
                        return
                    for raw in data.get("users") or []:
                        parsed = _profile_from_im(raw)
                        if parsed:
                            im_profiles.append(parsed)

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
            if not await self._inbox_loads(page):
                raise TikTokError("Your TikTok inbox didn't load.")
            try:
                await page.wait_for_function(
                    f"() => [...document.querySelectorAll(\"{CONVERSATION_NICKNAME}\")].some(e => e.innerText.trim())",
                    timeout=20000,
                )
            except PlaywrightTimeoutError:
                return []
            await page.wait_for_timeout(2000)  # let profile responses arrive
            rows = await page.evaluate(
                f"""() => [...document.querySelectorAll("{CONVERSATION_ITEM}")].map(r => ({{
                    nick: ((r.querySelector("{CONVERSATION_NICKNAME}") || {{}}).innerText || '').trim(),
                    src: [...r.querySelectorAll('img')].map(i => i.src).join(' '),
                }}))"""
            )
            return match_chat_rows(rows, im_profiles, limit)

    async def lookup_profile(self, cookies: list[dict], username: str) -> Profile | None:
        async with self._session(cookies) as ctx:
            page = await ctx.new_page()
            await page.goto(f"{BASE}/@{username}", wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):
                await page.wait_for_timeout(1000)
                if await page.locator(PROFILE_SUBTITLE).count():
                    subtitle = (await page.locator(PROFILE_SUBTITLE).first.inner_text()).strip()
                    title = (await page.locator(PROFILE_TITLE).first.inner_text()).strip() if \
                        await page.locator(PROFILE_TITLE).count() else subtitle
                    avatar = await page.locator(PROFILE_AVATAR).first.get_attribute("src") if \
                        await page.locator(PROFILE_AVATAR).count() else None
                    return profile_from_page(username, title, subtitle, avatar)
                if await page.get_by_text("Couldn't find this account").count():
                    return None
            raise TikTokError("The profile page didn't load.")

    async def send_messages(
        self, cookies: list[dict], usernames: list[str], message: str
    ) -> tuple[list[SendResult], list[dict]]:
        async with self._session(cookies) as ctx:
            check = await ctx.new_page()
            if await self._username_in(check) is None:
                raise SessionExpired("TikTok login expired.")
            await check.close()

            results: list[SendResult] = []
            for i, username in enumerate(usernames):
                if i:
                    await asyncio.sleep(random.uniform(3, 6))
                page = await ctx.new_page()
                try:
                    results.append(await self._send_one(page, username, message))
                finally:
                    await page.close()
            log.info("send run finished: %d/%d ok", sum(r.ok for r in results), len(results))
            return results, filter_tiktok_cookies(await ctx.cookies())

    async def _send_one(self, page: Page, username: str, message: str) -> SendResult:
        try:
            await page.goto(f"{BASE}/@{username}", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            if "/login" in page.url:
                raise SessionExpired("TikTok login expired.")
            if await page.get_by_text("Couldn't find this account").count():
                return SendResult(username, False, "not_found")
            await page.click(MESSAGE_BUTTON, timeout=30000)
            if "/login" in page.url:
                raise SessionExpired("TikTok login expired.")
            await page.click(CHAT_INPUT, timeout=30000)
            await page.keyboard.type(message, delay=50)
            await page.click(SEND_BUTTON, timeout=15000)
            await page.wait_for_timeout(1500)
            if await page.get_by_text("Something went wrong").count():
                return SendResult(username, False, "tiktok_error")
            return SendResult(username, True, None)
        except PlaywrightTimeoutError:
            if await page.get_by_text("Something went wrong").count():
                return SendResult(username, False, "tiktok_error")
            return SendResult(username, False, "ui_changed")

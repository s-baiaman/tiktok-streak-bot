"""Business logic behind the web panel: bots, their settings, and sending."""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TypeVar

from .crypto import CookieBox
from .db import Bot, Recipient, Repo
from .parsing import (
    ParseError,
    normalize_username,
    parse_send_time,
    parse_sessionid,
    parse_sid_guard_expiry,
    parse_timezone,
)
from .scheduling import (
    EXPIRY_WARNING_WINDOW,
    health_check_target,
    is_health_check_due,
    is_send_due,
    local_today,
)
from .tiktok.client import Profile, SendResult
from .tiktok.cookies import REGION_CODES, build_cookies, find_cookie
from .tiktok.errors import SessionExpired

log = logging.getLogger(__name__)

_REGION = re.compile(r"^[a-z0-9-]{2,20}$")
MAX_MESSAGE_LENGTH = 200
TEST_MESSAGE = "test"
# A scheduled send waits until the bot's settings have been left alone this long, so a send isn't
# triggered half-way through setting a bot up (e.g. before all recipients are ticked).
EDIT_SETTLE = timedelta(minutes=2)
T = TypeVar("T")

_ERROR_LABELS = {
    "not_found": "account not found",
    "ui_changed": "TikTok's page changed",
    "tiktok_error": "TikTok showed an error",
}


class BotNotFound(KeyError):
    """No bot with that id."""


class Busy(Exception):
    """The bot is already sending."""


class NoRecipients(Exception):
    """The bot has nobody to send to."""


class AddOutcome(Enum):
    ADDED = "added"
    UPDATED = "updated"
    BAD_COOKIE = "bad_cookie"
    NEEDS_REGION = "needs_region"


class _LoginFailed(Exception):
    def __init__(self, outcome: AddOutcome):
        self.outcome = outcome


def summarize(results: Sequence[SendResult]) -> str:
    ok = sum(1 for r in results if r.ok)
    text = f"Sent {ok}/{len(results)}"
    failed = [r for r in results if not r.ok]
    if failed:
        text += " · failed: " + ", ".join(
            f"@{r.username} ({_ERROR_LABELS.get(r.error, 'unknown error')})" for r in failed
        )
    return text


def _clean_message(text: str) -> str:
    message = (text or "").strip()
    if not message:
        raise ParseError("The message can't be empty.")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ParseError(f"Please keep the message under {MAX_MESSAGE_LENGTH} characters.")
    return message


def _clean_region(region: str | None) -> str | None:
    if region is None or not region.strip():
        return None
    region = region.strip().lower()
    if not _REGION.match(region):
        raise ParseError("That doesn't look like a tt-target-idc value (e.g. useast5).")
    return region


class PanelService:
    def __init__(
        self,
        repo: Repo,
        box: CookieBox,
        tiktok,
        default_timezone: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_change: Callable[[], None] = lambda: None,
    ):
        self.repo = repo
        self.box = box
        self.tiktok = tiktok
        self.default_timezone = default_timezone
        self._clock = clock
        self._on_change = on_change
        self._running: set[int] = set()
        self._last_edit: dict[int, datetime] = {}
        # Bumped whenever a bot's login is replaced; a run that started with an older login must not
        # overwrite the new one's cookies or health with its own outcome.
        self._login_gen: dict[int, int] = {}

    # --- helpers ----------------------------------------------------------------

    def _edited(self, bot_id: int) -> None:
        self._last_edit[bot_id] = self._clock()
        self._changed()

    def _settled(self, bot_id: int, now: datetime) -> bool:
        last = self._last_edit.get(bot_id)
        return last is None or now - last >= EDIT_SETTLE

    def _login_generation(self, bot_id: int) -> int:
        return self._login_gen.get(bot_id, 0)

    def _session_fields(self, bot_id: int, gen: int, ok: bool, refreshed: list[dict] | None = None) -> dict:
        """What a finished run may record about the login: nothing if the login was replaced while it
        ran (its outcome is about the old login), else session_ok plus any cookies TikTok refreshed."""
        if self._login_generation(bot_id) != gen:
            return {}
        fields: dict = {"session_ok": ok}
        if ok and refreshed is not None:
            fields.update(self._save_refreshed(bot_id, refreshed))
        return fields

    def _changed(self) -> None:
        try:
            self._on_change()
        except Exception:
            log.exception("change listener failed")

    def _cookies(self, bot_id: int) -> list[dict]:
        blob = self.repo.get_cookies_blob(bot_id)
        if blob is None:
            raise BotNotFound(bot_id)
        return self.box.open(blob)

    def _save_refreshed(self, bot_id: int, refreshed: list[dict]) -> dict:
        """Store cookies TikTok refreshed during a run; returns extra fields to record."""
        if not find_cookie(refreshed, "sessionid"):
            return {}
        if not self.repo.replace_cookies(bot_id, self.box.seal(refreshed)):
            return {}
        sid_guard = find_cookie(refreshed, "sid_guard")
        expires = parse_sid_guard_expiry(sid_guard) if sid_guard else None
        return {"session_expires_at": expires.isoformat()} if expires else {}

    async def _guarded(self, bot_id: int, work: Callable[[], Awaitable[T]]) -> T:
        if bot_id in self._running:
            raise Busy()
        self._running.add(bot_id)
        self._changed()
        try:
            return await work()
        finally:
            self._running.discard(bot_id)
            self._changed()

    # --- bots ----------------------------------------------------------------------

    def get_bot(self, bot_id: int) -> Bot:
        bot = self.repo.get_bot(bot_id)
        if bot is None:
            raise BotNotFound(bot_id)
        return bot

    def list_bots(self) -> list[Bot]:
        return self.repo.list_bots()

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self._running

    async def _login(self, sessionid_text: str, region: str | None) -> tuple[str, str, bytes]:
        """Log in with a pasted cookie. Returns (username, region, sealed cookies) or raises _LoginFailed."""
        sessionid = parse_sessionid(sessionid_text)
        region = _clean_region(region)
        username = await self.tiktok.whoami(build_cookies(sessionid, region or REGION_CODES[0]))
        if username is None:
            raise _LoginFailed(AddOutcome.BAD_COOKIE)
        if region is None:
            region = await self.tiktok.detect_region(sessionid)
            if region is None:
                raise _LoginFailed(AddOutcome.NEEDS_REGION)
        return username, region, self.box.seal(build_cookies(sessionid, region))

    def _apply_login(self, bot_id: int, region: str, blob: bytes) -> tuple[AddOutcome, Bot]:
        self._login_gen[bot_id] = self._login_generation(bot_id) + 1
        self.repo.replace_cookies(bot_id, blob)
        self.repo.update_fields(bot_id, region=region, session_ok=True, session_expires_at=None)
        log.info("bot id %s login refreshed", bot_id)
        self._changed()
        return AddOutcome.UPDATED, self.get_bot(bot_id)

    async def add_bot(self, sessionid_text: str, region: str | None = None) -> tuple[AddOutcome, Bot | None]:
        try:
            username, region, blob = await self._login(sessionid_text, region)
        except _LoginFailed as e:
            return e.outcome, None
        existing = self.repo.get_bot_by_username(username)
        if existing is None:
            try:
                created = self.repo.create_bot(username, region, self.default_timezone, blob, session_ok=True)
            except sqlite3.IntegrityError:  # the same account was added concurrently
                existing = self.repo.get_bot_by_username(username)
            else:
                log.info("bot id %s added", created.id)
                self._changed()
                return AddOutcome.ADDED, created
        return self._apply_login(existing.id, region, blob)

    async def update_login(
        self, bot_id: int, sessionid_text: str, region: str | None = None
    ) -> tuple[AddOutcome, Bot | None]:
        bot = self.get_bot(bot_id)
        try:
            username, region, blob = await self._login(sessionid_text, region)
        except _LoginFailed as e:
            return e.outcome, None
        if username != bot.tiktok_username:
            raise ParseError(f"That cookie belongs to @{username}, not @{bot.tiktok_username}.")
        return self._apply_login(bot_id, region, blob)

    def update_settings(
        self,
        bot_id: int,
        *,
        message: str | None = None,
        send_time: str | None = None,
        timezone: str | None = None,
        paused: bool | None = None,
    ) -> Bot:
        self.get_bot(bot_id)
        fields: dict = {}
        if message is not None:
            fields["message"] = _clean_message(message)
        if send_time is not None:
            fields["send_time"] = parse_send_time(send_time)
        if timezone is not None:
            fields["timezone"] = parse_timezone(timezone)
        if paused is not None:
            fields["paused"] = bool(paused)
        if fields:
            self.repo.update_fields(bot_id, **fields)
            self._edited(bot_id)
        return self.get_bot(bot_id)

    def recipients(self, bot_id: int) -> list[Recipient]:
        return self.repo.get_recipients(bot_id)

    def set_recipients(self, bot_id: int, recipients: list[Recipient]) -> None:
        self.get_bot(bot_id)
        cleaned: list[Recipient] = []
        seen: set[str] = set()
        for r in recipients:
            username = normalize_username(r.username)
            if username in seen:
                continue
            seen.add(username)
            cleaned.append(Recipient(username, (r.display_name or "").strip()[:100] or username))
        self.repo.set_recipients(bot_id, cleaned)
        self._edited(bot_id)

    def delete_bot(self, bot_id: int) -> None:
        self.get_bot(bot_id)
        self.repo.delete_bot(bot_id)
        log.info("bot id %s deleted", bot_id)
        self._changed()

    async def recent_chats(self, bot_id: int) -> list[Profile]:
        return await self.tiktok.list_recent_chats(self._cookies(bot_id))

    async def lookup(self, bot_id: int, username_text: str) -> Profile | None:
        username = normalize_username(username_text)
        return await self.tiktok.lookup_profile(self._cookies(bot_id), username)

    # --- status -----------------------------------------------------------------------

    def status(self, bot: Bot, now: datetime, recipients: list[Recipient] | None = None) -> tuple[str, str]:
        """(level, label) for the dashboard. Pass `recipients` if already loaded, to skip a query."""
        if self.is_running(bot.id):
            return "busy", "Sending…"
        if bot.session_ok is False:
            return "bad", "Login expired"
        if bot.last_run_ok is False:
            return "bad", "Last send failed"
        if bot.session_expires_at:
            expires = datetime.fromisoformat(bot.session_expires_at)
            if expires <= now + EXPIRY_WARNING_WINDOW:
                days = (expires - now).days
                if days <= 0:
                    return "warn", "Login expires today"
                return "warn", f"Login expires in {days} day{'s' if days != 1 else ''}"
        if bot.paused:
            return "off", "Paused"
        if not (recipients if recipients is not None else self.repo.get_recipients(bot.id)):
            return "warn", "No recipients"
        if not bot.send_time:
            return "warn", "No send time"
        return "ok", "Healthy"

    # --- sending -------------------------------------------------------------------------

    def ensure_can_send(self, bot_id: int) -> None:
        """Raise BotNotFound, Busy or NoRecipients if "Send now" can't start for this bot."""
        self.get_bot(bot_id)
        if self.is_running(bot_id):
            raise Busy()
        if not self.repo.get_recipients(bot_id):
            raise NoRecipients()

    def _record_run(self, bot_id: int, ok: bool, summary: str, **extra) -> None:
        # A no-op if the bot was deleted meanwhile.
        self.repo.update_fields(
            bot_id, last_run_ok=ok, last_run_at=self._clock().isoformat(), last_run_summary=summary, **extra
        )

    async def _run_daily(self, bot_id: int, scheduled: bool) -> None:
        bot = self.repo.get_bot(bot_id)  # current row: settings may have changed meanwhile
        if bot is None:
            return
        now = self._clock()
        recipients = self.repo.get_recipients(bot_id)
        if not recipients or (scheduled and not is_send_due(bot, now)):
            return
        if scheduled:
            # Claim the day before sending, so a crash or restart mid-run never sends twice.
            self.repo.update_fields(bot_id, last_run_date=local_today(bot, now).isoformat())
        gen = self._login_generation(bot_id)
        try:
            results, refreshed = await self.tiktok.send_messages(
                self._cookies(bot_id), [r.username for r in recipients], bot.message
            )
        except SessionExpired:
            log.info("bot id %s: login expired during run", bot_id)
            self._record_run(bot_id, False, "Failed: TikTok login expired", **self._session_fields(bot_id, gen, False))
        except Exception as e:
            log.error("bot id %s: run failed (%s)", bot_id, type(e).__name__)
            self._record_run(bot_id, False, "Failed: couldn't talk to TikTok")
        else:
            self._record_run(bot_id, all(r.ok for r in results), summarize(results),
                             **self._session_fields(bot_id, gen, True, refreshed))

    async def send_now(self, bot_id: int) -> None:
        self.ensure_can_send(bot_id)
        await self._guarded(bot_id, lambda: self._run_daily(bot_id, scheduled=False))

    async def test_send(self, bot_id: int, username_text: str, text: str | None = None) -> SendResult:
        self.get_bot(bot_id)
        username = normalize_username(username_text)
        message = _clean_message(TEST_MESSAGE if text is None else text)

        async def work() -> SendResult:
            gen = self._login_generation(bot_id)
            try:
                results, refreshed = await self.tiktok.send_messages(self._cookies(bot_id), [username], message)
            except SessionExpired:
                if fields := self._session_fields(bot_id, gen, False):
                    self.repo.update_fields(bot_id, **fields)
                raise
            if fields := self._session_fields(bot_id, gen, True, refreshed):
                self.repo.update_fields(bot_id, **fields)
            return results[0]

        return await self._guarded(bot_id, work)

    async def _health_check(self, bot: Bot, now: datetime) -> None:
        self.repo.update_fields(bot.id, last_health_date=health_check_target(bot, now).isoformat())
        gen = self._login_generation(bot.id)
        try:
            ok = await self.tiktok.whoami(self._cookies(bot.id)) is not None
        except SessionExpired:
            ok = False
        except Exception as e:
            # TikTok unreachable is not proof the login is dead. The send itself will tell.
            log.warning("bot id %s: health check inconclusive (%s)", bot.id, type(e).__name__)
            return
        if not ok and (fields := self._session_fields(bot.id, gen, False)):
            log.info("bot id %s: health check found an expired login", bot.id)
            self.repo.update_fields(bot.id, **fields)
            self._changed()

    async def _tick_bot(self, bot_id: int, now: datetime) -> None:
        bot = self.repo.get_bot(bot_id)  # fresh: earlier bots' runs may have taken minutes
        if bot is not None and is_health_check_due(bot, now):
            await self._health_check(bot, now)
            bot = self.repo.get_bot(bot_id)
        if bot is None or not is_send_due(bot, now) or not self._settled(bot_id, now) \
                or self.is_running(bot_id) or not self.repo.get_recipients(bot_id):
            return
        await self._guarded(bot_id, lambda: self._run_daily(bot_id, scheduled=True))

    async def tick(self, now: datetime) -> None:
        for bot in self.repo.list_bots():
            try:
                await self._tick_bot(bot.id, now)
            except Exception as e:
                log.error("tick failed for bot id %s (%s)", bot.id, type(e).__name__)

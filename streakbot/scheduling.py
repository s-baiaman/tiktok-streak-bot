"""Pure 'is it time?' rules. `now` is always a UTC-aware datetime."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import Bot

HEALTH_CHECK_LEAD = timedelta(hours=3)
EXPIRY_WARNING_WINDOW = timedelta(days=7)


def is_runnable(bot: Bot) -> bool:
    return not bot.paused and bool(bot.send_time) and bool(bot.timezone)


def local_today(bot: Bot, now: datetime) -> date:
    return now.astimezone(ZoneInfo(bot.timezone)).date()


def _send_at(bot: Bot, day: date) -> datetime:
    return datetime.combine(day, time.fromisoformat(bot.send_time), tzinfo=ZoneInfo(bot.timezone))


def is_send_due(bot: Bot, now: datetime) -> bool:
    if not is_runnable(bot):
        return False
    today = local_today(bot, now)
    if bot.last_run_date == today.isoformat():
        return False
    return now >= _send_at(bot, today)


def health_check_target(bot: Bot, now: datetime) -> date:
    """The local date of the next send that hasn't happened yet."""
    today = local_today(bot, now)
    return today if bot.last_run_date != today.isoformat() else today + timedelta(days=1)


def is_health_check_due(bot: Bot, now: datetime) -> bool:
    if not is_runnable(bot):
        return False
    target = health_check_target(bot, now)
    if bot.last_health_date == target.isoformat():
        return False
    send_at = _send_at(bot, target)
    return send_at - HEALTH_CHECK_LEAD <= now < send_at


def next_send_at(bot: Bot, now: datetime) -> datetime | None:
    """When the next scheduled send happens (may be in the past if it is due right now)."""
    if not is_runnable(bot):
        return None
    return _send_at(bot, health_check_target(bot, now))

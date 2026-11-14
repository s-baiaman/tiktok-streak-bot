"""Pure parsing/normalization of user input. Errors carry user-facing English."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import cache
from urllib.parse import unquote_plus
from zoneinfo import available_timezones


class ParseError(ValueError):
    """Input couldn't be understood. str(e) is safe to show the user (never echoes secrets)."""


_SESSIONID_VALUE = re.compile(r"^[A-Za-z0-9]{16,64}$")
_SESSIONID_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_])sessionid(?:\s*=\s*|\t+)([A-Za-z0-9]+)")


def _sessionid_from_json(text: str) -> str | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and item.get("name") == "sessionid":
            return str(item.get("value", ""))
    return None


def parse_sessionid(text: str) -> str:
    cleaned = text.strip().strip("'\"").strip()
    candidate: str | None = None
    if cleaned.startswith(("[", "{")):
        candidate = _sessionid_from_json(cleaned)
    if candidate is None:
        m = _SESSIONID_IN_TEXT.search(cleaned)
        candidate = m.group(1) if m else cleaned
    if not _SESSIONID_VALUE.match(candidate):
        raise ParseError(
            "That doesn't look like a sessionid. It should be a long string of letters and "
            "numbers copied from the 'sessionid' cookie."
        )
    return candidate


_USERNAME = re.compile(r"^[a-z0-9._]{2,24}$")


def normalize_username(text: str) -> str:
    value = text.strip()
    if "tiktok.com/@" in value:
        value = value.split("tiktok.com/@", 1)[1]
    value = value.lstrip("@")
    value = re.split(r"[/?#]", value, maxsplit=1)[0].strip().lower()
    if not _USERNAME.match(value):
        raise ParseError(
            "That isn't a valid TikTok username. Use the @username (letters, numbers, '.' and '_'), "
            "not the display name."
        )
    return value


_TIME = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?$")


def parse_send_time(text: str) -> str:
    m = _TIME.match(text.strip().lower())
    error = ParseError("Please send a time like 20:00 or 8pm.")
    if not m:
        raise error
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise error
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif m.group(2) is None:
        raise error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise error
    return f"{hour:02d}:{minute:02d}"


_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$")


@cache
def _zones_by_lower() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def parse_timezone(text: str) -> str:
    value = text.strip()
    if not value:
        raise ParseError("Please send a timezone like Europe/Moscow or an offset like +3.")
    m = _OFFSET.match(value.lower())
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if minutes:
            raise ParseError(
                "Half-hour offsets aren't supported. Send a city timezone instead, e.g. Asia/Kolkata."
            )
        offset = hours if sign == "+" else -hours
        if not -12 <= offset <= 14:
            raise ParseError("That offset doesn't exist. Offsets go from -12 to +14.")
        if offset == 0:
            return "UTC"
        # POSIX Etc/ zones have inverted signs: UTC+3 is Etc/GMT-3.
        return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
    name = _zones_by_lower().get(value.lower())
    if name is None:
        raise ParseError(
            "I don't know that timezone. Use a name like Europe/Moscow or America/New_York, "
            "or an offset like +3."
        )
    return name


def parse_sid_guard_expiry(value: str) -> datetime | None:
    """sid_guard = <token>|<issued_unix>|<max_age_seconds>|<http-date>, URL-encoded."""
    try:
        parts = unquote_plus(value).split("|")
        if len(parts) < 3:
            return None
        return datetime.fromtimestamp(int(parts[1]) + int(parts[2]), UTC)
    except (ValueError, OverflowError, OSError):
        return None

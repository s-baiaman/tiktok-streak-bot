"""Settings loaded from environment variables (written to .env by install.sh)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Invalid or missing configuration. The message is shown to the host."""


@dataclass(frozen=True)
class Settings:
    password_hash: str
    port: int
    default_timezone: str
    data_dir: Path


def _get(env: Mapping[str, str], key: str) -> str | None:
    value = (env.get(key) or "").strip()
    return value or None


def load_settings(env: Mapping[str, str]) -> Settings:
    password_hash = _get(env, "PANEL_PASSWORD_HASH")
    if password_hash and len(password_hash) >= 2 and password_hash[0] == password_hash[-1] and password_hash[0] in "'\"":
        password_hash = password_hash[1:-1]  # .env quoting that reached us literally
    if not password_hash:
        raise ConfigError("PANEL_PASSWORD_HASH is missing. Run ./install.sh to set the panel password.")

    raw_port = _get(env, "PANEL_PORT") or "8080"
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise ConfigError(f"PANEL_PORT must be a number from 1 to 65535, got {raw_port!r}.")

    default_timezone = _get(env, "DEFAULT_TIMEZONE") or "UTC"
    try:
        ZoneInfo(default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"DEFAULT_TIMEZONE {default_timezone!r} is not a valid timezone name.") from None

    return Settings(
        password_hash=password_hash,
        port=int(raw_port),
        default_timezone=default_timezone,
        data_dir=Path(_get(env, "DATA_DIR") or "data"),
    )

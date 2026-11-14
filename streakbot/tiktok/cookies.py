"""Building and filtering the Playwright cookies the bot uses."""
from __future__ import annotations

# Data-center codes for the tt-target-idc cookie. The first two are verified to
# work (2026-11-08); for US-east accounts the exact value didn't matter.
REGION_CODES: tuple[str, ...] = ("useast5", "useast8", "useast2a", "useast1a", "alisg", "maliva", "no1a")


def _cookie(name: str, value: str) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": ".tiktok.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def build_cookies(sessionid: str, region: str) -> list[dict]:
    return [_cookie("sessionid", sessionid), _cookie("tt-target-idc", region)]


def _is_tiktok_domain(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == "tiktok.com" or d.endswith(".tiktok.com")


def filter_tiktok_cookies(cookies: list[dict]) -> list[dict]:
    return [c for c in cookies if _is_tiktok_domain(c.get("domain", ""))]


def find_cookie(cookies: list[dict], name: str) -> str | None:
    for c in cookies:
        if c.get("name") == name:
            return c.get("value")
    return None

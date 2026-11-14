from streakbot.tiktok.cookies import REGION_CODES, build_cookies, filter_tiktok_cookies, find_cookie
from streakbot.tiktok.errors import RecipientNotFound, SessionExpired, TikTokError, UiChanged


def test_build_cookies():
    cs = build_cookies("abc123", "useast5")
    assert {c["name"]: c["value"] for c in cs} == {"sessionid": "abc123", "tt-target-idc": "useast5"}
    assert all(c["domain"] == ".tiktok.com" and c["path"] == "/" and c["secure"] and c["httpOnly"] for c in cs)
    assert all(c["sameSite"] == "Lax" and c["expires"] == -1 for c in cs)


def test_region_codes_start_with_verified_us_east():
    assert REGION_CODES[:2] == ("useast5", "useast8")


def test_filter_and_find():
    cs = [
        {"name": "a", "value": "1", "domain": ".tiktok.com"},
        {"name": "b", "value": "2", "domain": ".google.com"},
        {"name": "sid_guard", "value": "x", "domain": "www.tiktok.com"},
        {"name": "c", "value": "3", "domain": "eviltiktok.com.attacker.net"},
    ]
    assert [c["name"] for c in filter_tiktok_cookies(cs)] == ["a", "sid_guard"]
    assert find_cookie(cs, "sid_guard") == "x"
    assert find_cookie(cs, "zzz") is None


def test_error_hierarchy():
    for cls in (SessionExpired, RecipientNotFound, UiChanged):
        assert issubclass(cls, TikTokError)


def test_errors_do_not_leak_values():
    assert "abc123" not in str(SessionExpired("TikTok login expired"))


# --- review fixes: client boundary ----------------------------------------------
import pytest
from playwright.async_api import Error as PlaywrightError

from streakbot.tiktok.client import TikTokClient, interpret_account_info, profile_from_page


def test_account_info_logged_in():
    assert interpret_account_info({"status": 200, "json": {"message": "success", "data": {"username": "u1"}}}) == "u1"


def test_account_info_logged_out_is_none():
    assert interpret_account_info({"status": 200, "json": {"message": "error", "data": {"description": "login"}}}) is None
    assert interpret_account_info({"status": 200, "json": {"message": "success", "data": {}}}) is None


@pytest.mark.parametrize("result", [None, {"status": 429, "json": None}, {"status": 200, "json": None},
                                    {"status": 503, "json": {"message": "error"}}])
def test_account_info_ambiguous_raises(result):                       # I5
    with pytest.raises(TikTokError):
        interpret_account_info(result)


def test_profile_from_page_uses_requested_username():                 # I6
    p = profile_from_page("sam.r_07", "Sam", "sam.r_07", None)
    assert (p.username, p.display_name) == ("sam.r_07", "Sam")


def test_profile_from_page_mismatch_raises():                         # I6
    with pytest.raises(UiChanged):
        profile_from_page("sam.r_07", "sam.r_07", "Sam", None)


async def test_playwright_errors_become_tiktok_errors(monkeypatch):   # I4
    client = TikTokClient()

    async def boom():
        raise PlaywrightError("browser crashed")
    monkeypatch.setattr(client, "start", boom)
    with pytest.raises(TikTokError):
        await client.whoami([])

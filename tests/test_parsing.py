from datetime import UTC, datetime

import pytest

from streakbot.parsing import (
    ParseError,
    normalize_username,
    parse_send_time,
    parse_sessionid,
    parse_sid_guard_expiry,
    parse_timezone,
)

SID = "abc123def456abc1"


@pytest.mark.parametrize("raw", [
    SID,
    f"  {SID}\n",
    f'"{SID}"',
    f"sessionid={SID}",
    f"sessionid={SID};",
    f"foo=1; sessionid={SID}; bar=2",
    f"Cookie: tt=1; sessionid={SID}",
    f'[{{"name":"x","value":"1"}},{{"name":"sessionid","value":"{SID}"}}]',
    f'{{"name":"sessionid","value":"{SID}"}}',
    f"sessionid\t{SID}\t.tiktok.com\t/",
])
def test_parse_sessionid_variants(raw):
    assert parse_sessionid(raw) == SID


def test_parse_sessionid_does_not_pick_sessionid_ss():
    assert parse_sessionid(f"sessionid_ss=zzzzzzzzzzzzzzzzzz; sessionid={SID}") == SID


@pytest.mark.parametrize("raw", ["", "   ", "hello world", "sessionid=", "short", "sid_tt=" + SID])
def test_parse_sessionid_rejects(raw):
    with pytest.raises(ParseError):
        parse_sessionid(raw)


def test_parse_error_does_not_echo_input():
    with pytest.raises(ParseError) as e:
        parse_sessionid("not a cookie at all but secret-ish")
    assert "secret-ish" not in str(e.value)


@pytest.mark.parametrize("raw,expected", [
    ("sam.r_07", "sam.r_07"),
    ("@Sam.R_07 ", "sam.r_07"),
    ("https://www.tiktok.com/@jordan_9", "jordan_9"),
    ("https://www.tiktok.com/@jordan_9?lang=en", "jordan_9"),
    ("tiktok.com/@jordan_9/video/123", "jordan_9"),
    ("user.name_1", "user.name_1"),
])
def test_normalize_username(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "@", "bad name", "x" * 25, "émoji", "a"])
def test_normalize_username_rejects(raw):
    with pytest.raises(ParseError):
        normalize_username(raw)


@pytest.mark.parametrize("raw,expected", [
    ("20:00", "20:00"), ("8:05", "08:05"), ("08.05", "08:05"), ("8pm", "20:00"),
    ("8 PM", "20:00"), ("12am", "00:00"), ("12pm", "12:00"), ("7:30 am", "07:30"), ("0:00", "00:00"),
])
def test_parse_send_time(raw, expected):
    assert parse_send_time(raw) == expected


@pytest.mark.parametrize("raw", ["24:00", "7:60", "noon", "", "13pm", "0am"])
def test_parse_send_time_rejects(raw):
    with pytest.raises(ParseError):
        parse_send_time(raw)


@pytest.mark.parametrize("raw,expected", [
    ("Europe/Moscow", "Europe/Moscow"),
    ("europe/moscow", "Europe/Moscow"),
    (" Asia/Bishkek ", "Asia/Bishkek"),
    ("UTC", "UTC"),
    ("utc", "UTC"),
    ("+3", "Etc/GMT-3"),
    ("UTC+3", "Etc/GMT-3"),
    ("GMT-5", "Etc/GMT+5"),
    ("+0", "UTC"),
    ("UTC+14", "Etc/GMT-14"),
])
def test_parse_timezone(raw, expected):
    assert parse_timezone(raw) == expected


@pytest.mark.parametrize("raw", ["Mars/Base", "+15", "+3:30", "", "-13"])
def test_parse_timezone_rejects(raw):
    with pytest.raises(ParseError):
        parse_timezone(raw)


def test_half_hour_offset_hint_mentions_city():
    with pytest.raises(ParseError, match="Asia/Kolkata"):
        parse_timezone("+5:30")


def test_sid_guard_expiry():
    v = "abc%7C1751500000%7C15552000%7CMon%2C+29-Dec-2025+00%3A00%3A00+GMT"
    assert parse_sid_guard_expiry(v) == datetime.fromtimestamp(1751500000 + 15552000, UTC)


def test_sid_guard_expiry_unencoded():
    assert parse_sid_guard_expiry("abc|100|200|whatever") == datetime.fromtimestamp(300, UTC)


@pytest.mark.parametrize("raw", ["nonsense", "", "a|b|c|d", "a|1"])
def test_sid_guard_garbage(raw):
    assert parse_sid_guard_expiry(raw) is None

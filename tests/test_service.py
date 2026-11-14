from datetime import UTC, datetime, timedelta

import pytest

from fakes import FakeTikTok
from streakbot.crypto import CookieBox, load_or_create_key
from streakbot.db import Recipient, Repo
from streakbot.parsing import ParseError
from streakbot.service import (
    AddOutcome,
    BotNotFound,
    Busy,
    NoRecipients,
    PanelService,
    summarize,
)
from streakbot.tiktok.client import Profile, SendResult
from streakbot.tiktok.cookies import find_cookie
from streakbot.tiktok.errors import TikTokError

SID = "abc123def456abc1"
NOW = datetime(2026, 11, 8, 17, 30, tzinfo=UTC)  # 20:30 in Moscow


@pytest.fixture
def tiktok():
    return FakeTikTok()


@pytest.fixture
def svc(tmp_path, tiktok):
    clock = {"now": NOW}
    changes = []
    s = PanelService(
        Repo(tmp_path / "db.sqlite"), CookieBox(load_or_create_key(tmp_path / "k")), tiktok, "Europe/Moscow",
        clock=lambda: clock["now"], on_change=lambda: changes.append(1),
    )
    s._clock_box = clock
    s._changes = changes
    return s


async def ready_bot(svc, recipients=("sam.r_07",), send_time="20:00"):
    svc._clock_box["now"] = NOW - timedelta(minutes=5)
    outcome, bot = await svc.add_bot(SID)
    assert outcome is AddOutcome.ADDED
    svc.set_recipients(bot.id, [Recipient(r, r.upper()) for r in recipients])
    bot = svc.update_settings(bot.id, send_time=send_time)
    svc._clock_box["now"] = NOW
    return bot


def sends(tiktok):
    return [c for c in tiktok.calls if c[0] == "send"]


# --- adding ----------------------------------------------------------------------

async def test_add_bot_defaults(svc):
    outcome, bot = await svc.add_bot(SID)
    assert outcome is AddOutcome.ADDED
    assert bot.tiktok_username == "owner.acct"
    assert bot.message == "🔥" and bot.timezone == "Europe/Moscow" and bot.region == "useast5"
    assert bot.session_ok is True
    blob = svc.repo.get_cookies_blob(bot.id)
    assert SID.encode() not in blob
    assert find_cookie(svc.box.open(blob), "sessionid") == SID
    assert svc._changes


async def test_add_bot_accepts_cookie_header(svc):
    outcome, bot = await svc.add_bot(f"Cookie: tt=1; sessionid={SID}; other=2")
    assert outcome is AddOutcome.ADDED


async def test_add_bot_garbage_is_parse_error(svc):
    with pytest.raises(ParseError):
        await svc.add_bot("hello there")


async def test_add_bot_bad_cookie(svc, tiktok):
    tiktok.whoami_result = None
    assert await svc.add_bot(SID) == (AddOutcome.BAD_COOKIE, None)
    assert svc.list_bots() == []


async def test_add_bot_needs_region_then_region_given(svc, tiktok):
    tiktok.region = None
    assert await svc.add_bot(SID) == (AddOutcome.NEEDS_REGION, None)
    outcome, bot = await svc.add_bot(SID, region=" Alisg ")
    assert outcome is AddOutcome.ADDED and bot.region == "alisg"


async def test_add_bot_bad_region(svc):
    with pytest.raises(ParseError):
        await svc.add_bot(SID, region="no spaces allowed")


async def test_readding_same_account_updates(svc):
    _, bot = await svc.add_bot(SID)
    svc.repo.update_fields(bot.id, session_ok=False, session_expires_at="2026-11-10T00:00:00+00:00")
    outcome, again = await svc.add_bot("zzz999zzz999zzz9")
    assert outcome is AddOutcome.UPDATED and again.id == bot.id
    assert again.session_ok is True and again.session_expires_at is None
    assert len(svc.list_bots()) == 1
    assert find_cookie(svc.box.open(svc.repo.get_cookies_blob(bot.id)), "sessionid") == "zzz999zzz999zzz9"


async def test_update_login_wrong_account(svc, tiktok):
    _, bot = await svc.add_bot(SID)
    tiktok.whoami_result = "someone.else"
    with pytest.raises(ParseError, match="@someone.else"):
        await svc.update_login(bot.id, "zzz999zzz999zzz9")
    assert find_cookie(svc.box.open(svc.repo.get_cookies_blob(bot.id)), "sessionid") == SID


async def test_update_login_ok(svc):
    _, bot = await svc.add_bot(SID)
    svc.repo.update_fields(bot.id, session_ok=False)
    outcome, got = await svc.update_login(bot.id, "zzz999zzz999zzz9")
    assert outcome is AddOutcome.UPDATED and got.session_ok is True


async def test_unknown_bot(svc):
    with pytest.raises(BotNotFound):
        svc.get_bot(42)
    with pytest.raises(BotNotFound):
        svc.update_settings(42, message="x")


# --- settings ----------------------------------------------------------------------

async def test_update_settings(svc):
    _, bot = await svc.add_bot(SID)
    got = svc.update_settings(bot.id, message="  yo 🔥 ", send_time="8pm", timezone="+3", paused=True)
    assert (got.message, got.send_time, got.timezone, got.paused) == ("yo 🔥", "20:00", "Etc/GMT-3", True)
    got = svc.update_settings(bot.id, paused=False)
    assert got.paused is False and got.message == "yo 🔥"


@pytest.mark.parametrize("kwargs", [
    {"message": "   "}, {"message": "x" * 201}, {"send_time": "25:00"}, {"timezone": "Mars/Base"},
])
async def test_update_settings_validation(svc, kwargs):
    _, bot = await svc.add_bot(SID)
    with pytest.raises(ParseError):
        svc.update_settings(bot.id, **kwargs)


async def test_set_recipients_normalizes_and_dedupes(svc):
    _, bot = await svc.add_bot(SID)
    svc.set_recipients(bot.id, [Recipient("@Sam.R_07", "Sam"), Recipient("jordan_9", "J"), Recipient("sam.r_07", "dup")])
    assert svc.recipients(bot.id) == [Recipient("sam.r_07", "Sam"), Recipient("jordan_9", "J")]


async def test_set_recipients_bad_username(svc):
    _, bot = await svc.add_bot(SID)
    with pytest.raises(ParseError):
        svc.set_recipients(bot.id, [Recipient("bad name", "x")])


async def test_delete_bot(svc):
    bot = await ready_bot(svc)
    svc.delete_bot(bot.id)
    assert svc.list_bots() == []
    with pytest.raises(BotNotFound):
        svc.delete_bot(bot.id)


async def test_recent_chats_and_lookup(svc, tiktok):
    _, bot = await svc.add_bot(SID)
    tiktok.chats = [Profile("sam.r_07", "Sam", None)]
    tiktok.profiles = {"kai.m": Profile("kai.m", "Kai", None)}
    assert await svc.recent_chats(bot.id) == tiktok.chats
    assert (await svc.lookup(bot.id, "@Kai.M")).display_name == "Kai"
    assert await svc.lookup(bot.id, "nobody.here") is None


# --- scheduled runs ------------------------------------------------------------------

async def test_tick_sends_once_and_claims_day(svc, tiktok):
    bot = await ready_bot(svc)
    await svc.tick(NOW)
    await svc.tick(NOW + timedelta(minutes=1))
    assert sends(tiktok) == [("send", ("sam.r_07",), "🔥", SID)]
    got = svc.get_bot(bot.id)
    assert got.last_run_date == "2026-11-08" and got.last_run_ok is True
    assert got.last_run_summary == "Sent 1/1" and got.last_run_at == NOW.isoformat()


async def test_tick_skips_paused_and_empty(svc, tiktok):
    bot = await ready_bot(svc)
    svc.update_settings(bot.id, paused=True)
    await svc.tick(NOW)
    svc.update_settings(bot.id, paused=False)
    svc.set_recipients(bot.id, [])
    await svc.tick(NOW)
    assert sends(tiktok) == []


async def test_time_change_takes_effect_today(svc, tiktok):
    bot = await ready_bot(svc, send_time="21:00")  # 21:00 Moscow = 18:00 UTC, not yet
    await svc.tick(NOW)
    assert sends(tiktok) == []
    svc.update_settings(bot.id, send_time="20:15")
    await svc.tick(NOW + timedelta(minutes=2))
    assert len(sends(tiktok)) == 1


async def test_scheduled_send_waits_for_edits_to_settle(svc, tiktok):
    """Setting a past time and then adding recipients one by one must not send to only the first one."""
    _, bot = await svc.add_bot(SID)
    svc.update_settings(bot.id, send_time="20:00")  # already past (20:30 Moscow)
    svc.set_recipients(bot.id, [Recipient("sam.r_07", "Sam")])
    await svc.tick(NOW)
    assert sends(tiktok) == []
    svc._clock_box["now"] = NOW + timedelta(minutes=1)
    svc.set_recipients(bot.id, [Recipient("sam.r_07", "Sam"), Recipient("kai.m", "Kai")])
    await svc.tick(NOW + timedelta(minutes=2))
    assert sends(tiktok) == []
    await svc.tick(NOW + timedelta(minutes=3))
    assert sends(tiktok) == [("send", ("sam.r_07", "kai.m"), "🔥", SID)]


async def test_login_update_during_send_is_kept(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.expire = True  # the old login is dead

    async def update():
        await svc.update_login(bot.id, "new0new0new0new0")

    tiktok.on_send = update
    await svc.tick(NOW)
    got = svc.get_bot(bot.id)
    assert got.session_ok is True
    assert find_cookie(svc.box.open(svc.repo.get_cookies_blob(bot.id)), "sessionid") == "new0new0new0new0"


async def test_login_update_during_send_not_overwritten_by_refreshed_cookies(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.refreshed = [{"name": "sessionid", "value": "oldsession0old00", "domain": ".tiktok.com", "path": "/"}]

    async def update():
        await svc.update_login(bot.id, "new0new0new0new0")

    tiktok.on_send = update
    await svc.send_now(bot.id)
    assert find_cookie(svc.box.open(svc.repo.get_cookies_blob(bot.id)), "sessionid") == "new0new0new0new0"
    assert svc.get_bot(bot.id).last_run_summary == "Sent 1/1"


async def test_login_update_during_health_check_is_kept(svc, tiktok):
    bot = await ready_bot(svc, send_time="23:00")
    svc.repo.update_fields(bot.id, session_ok=False)

    async def update():
        tiktok.whoami_result = "owner.acct"
        await svc.update_login(bot.id, "new0new0new0new0")
        tiktok.whoami_result = None  # the health check's own call sees the old, dead login

    tiktok.on_whoami = update
    await svc.tick(NOW)
    assert svc.get_bot(bot.id).session_ok is True


async def test_claimed_day_survives_crash(svc, tiktok):
    bot = await ready_bot(svc)

    async def crash():
        raise SystemExit  # simulates the process dying mid-run

    tiktok.on_send = crash
    with pytest.raises(SystemExit):
        await svc.tick(NOW)
    tiktok.on_send = None
    await svc.tick(NOW)
    assert len(sends(tiktok)) == 1
    assert svc.get_bot(bot.id).last_run_date == "2026-11-08"


async def test_session_expired_during_run(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.expire = True
    await svc.tick(NOW)
    got = svc.get_bot(bot.id)
    assert got.session_ok is False and got.last_run_ok is False
    assert svc.status(got, NOW) == ("bad", "Login expired")


async def test_generic_failure_during_run(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.send_raises = TikTokError("boom")
    await svc.tick(NOW)
    got = svc.get_bot(bot.id)
    assert got.last_run_ok is False and got.last_run_summary == "Failed: couldn't talk to TikTok"
    assert svc.status(got, NOW) == ("bad", "Last send failed")


async def test_settings_edit_during_send_survives(svc, tiktok):
    bot = await ready_bot(svc)

    async def edit():
        svc.update_settings(bot.id, message="changed", paused=True)

    tiktok.on_send = edit
    await svc.tick(NOW)
    got = svc.get_bot(bot.id)
    assert got.message == "changed" and got.paused is True and got.last_run_ok is True


async def test_delete_during_send(svc, tiktok):
    bot = await ready_bot(svc)

    async def delete():
        svc.delete_bot(bot.id)

    tiktok.on_send = delete
    await svc.tick(NOW)
    assert svc.list_bots() == []
    assert svc.repo.get_bot(bot.id) is None


async def test_refreshed_cookies_and_expiry_saved(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.refreshed = [
        {"name": "sessionid", "value": "fresh0fresh0fresh", "domain": ".tiktok.com", "path": "/"},
        {"name": "sid_guard", "value": "x%7C1751500000%7C15552000%7Cdate", "domain": ".tiktok.com", "path": "/"},
    ]
    await svc.tick(NOW)
    got = svc.get_bot(bot.id)
    assert find_cookie(svc.box.open(svc.repo.get_cookies_blob(bot.id)), "sessionid") == "fresh0fresh0fresh"
    assert got.session_expires_at == datetime.fromtimestamp(1751500000 + 15552000, UTC).isoformat()


async def test_health_check_failure_marks_expired(svc, tiktok):
    bot = await ready_bot(svc, send_time="23:00")  # window 20:00–23:00 Moscow, now 20:30
    tiktok.whoami_result = None
    await svc.tick(NOW)
    assert svc.get_bot(bot.id).session_ok is False


async def test_health_check_unreachable_is_not_expiry(svc, tiktok):
    bot = await ready_bot(svc, send_time="23:00")
    tiktok.whoami_raises = TikTokError("network")
    await svc.tick(NOW)
    got = svc.get_bot(bot.id)
    assert got.session_ok is True and got.last_health_date == "2026-11-08"


# --- manual runs -----------------------------------------------------------------------

async def test_send_now_does_not_claim_day(svc, tiktok):
    bot = await ready_bot(svc, send_time="23:00")
    await svc.send_now(bot.id)
    got = svc.get_bot(bot.id)
    assert got.last_run_date is None and got.last_run_summary == "Sent 1/1"
    assert len(sends(tiktok)) == 1


async def test_send_now_without_recipients(svc):
    _, bot = await svc.add_bot(SID)
    with pytest.raises(NoRecipients):
        await svc.send_now(bot.id)


async def test_overlapping_runs_are_busy(svc, tiktok):
    bot = await ready_bot(svc)
    errors = []

    async def overlap():
        for call in (svc.send_now(bot.id), svc.test_send(bot.id, "kai.m")):
            try:
                await call
            except Busy:
                errors.append("busy")

    tiktok.on_send = overlap
    await svc.send_now(bot.id)
    assert errors == ["busy", "busy"]
    assert svc.is_running(bot.id) is False


async def test_running_flag_and_events(svc, tiktok):
    bot = await ready_bot(svc)
    seen = []

    async def look():
        seen.append(svc.is_running(bot.id))
        seen.append(svc.status(svc.get_bot(bot.id), NOW))

    tiktok.on_send = look
    before = len(svc._changes)
    await svc.send_now(bot.id)
    assert seen == [True, ("busy", "Sending…")]
    assert len(svc._changes) >= before + 2


async def test_test_send(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.outcomes = {"nobody.here": "not_found"}
    ok = await svc.test_send(bot.id, "@Kai.M")
    bad = await svc.test_send(bot.id, "nobody.here", "hello")
    assert ok == SendResult("kai.m", True, None)
    assert bad.ok is False
    assert sends(tiktok) == [("send", ("kai.m",), "test", SID), ("send", ("nobody.here",), "hello", SID)]
    got = svc.get_bot(bot.id)
    assert got.last_run_summary is None and got.last_run_date is None


async def test_test_send_validates(svc):
    bot = await ready_bot(svc)
    with pytest.raises(ParseError):
        await svc.test_send(bot.id, "bad name")
    with pytest.raises(ParseError):
        await svc.test_send(bot.id, "kai.m", "  ")


async def test_test_send_expired_session(svc, tiktok):
    bot = await ready_bot(svc)
    tiktok.expire = True
    from streakbot.tiktok.errors import SessionExpired
    with pytest.raises(SessionExpired):
        await svc.test_send(bot.id, "kai.m")
    assert svc.get_bot(bot.id).session_ok is False


# --- status & summary --------------------------------------------------------------------

async def test_status_order(svc):
    bot = await ready_bot(svc)
    st = lambda: svc.status(svc.get_bot(bot.id), NOW)
    assert st() == ("ok", "Healthy")
    svc.repo.update_fields(bot.id, session_expires_at=(NOW + timedelta(days=5, hours=1)).isoformat())
    assert st() == ("warn", "Login expires in 5 days")
    svc.update_settings(bot.id, paused=True)
    assert st() == ("warn", "Login expires in 5 days")
    svc.repo.update_fields(bot.id, session_expires_at=None)
    assert st() == ("off", "Paused")
    svc.update_settings(bot.id, paused=False)
    svc.set_recipients(bot.id, [])
    assert st() == ("warn", "No recipients")
    svc.repo.update_fields(bot.id, last_run_ok=False)
    assert st() == ("bad", "Last send failed")
    svc.repo.update_fields(bot.id, session_ok=False)
    assert st() == ("bad", "Login expired")


async def test_status_no_send_time(svc):
    _, bot = await svc.add_bot(SID)
    svc.set_recipients(bot.id, [Recipient("a.b", "A")])
    assert svc.status(svc.get_bot(bot.id), NOW) == ("warn", "No send time")


def test_summarize():
    assert summarize([SendResult("a", True, None), SendResult("b", True, None)]) == "Sent 2/2"
    assert summarize([SendResult("a", True, None), SendResult("b", False, "not_found")]) == \
        "Sent 1/2 · failed: @b (account not found)"
    assert summarize([SendResult("c", False, "weird")]) == "Sent 0/1 · failed: @c (unknown error)"

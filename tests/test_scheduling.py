from datetime import UTC, date, datetime

from streakbot.db import Bot
from streakbot.scheduling import (
    is_health_check_due,
    is_runnable,
    is_send_due,
    local_today,
    next_send_at,
)


def mk(**overrides) -> Bot:
    base = dict(id=1, tiktok_username="owner.acct", send_time="20:00", timezone="Europe/Moscow")
    base.update(overrides)
    return Bot(**base)


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_local_today():
    assert local_today(mk(), at(2026, 11, 8, 21, 30)) == date(2026, 11, 9)


def test_runnable():
    assert is_runnable(mk())
    assert not is_runnable(mk(paused=True))
    assert not is_runnable(mk(send_time=None))
    assert not is_runnable(mk(timezone=None))


def test_not_due_before_time():
    assert not is_send_due(mk(), at(2026, 11, 8, 16, 59))  # 19:59 Moscow


def test_due_at_time():
    assert is_send_due(mk(), at(2026, 11, 8, 17, 0))


def test_catch_up_same_day_after_downtime():
    assert is_send_due(mk(), at(2026, 11, 8, 20, 30))  # 23:30 local


def test_never_twice_a_day():
    assert not is_send_due(mk(last_run_date="2026-11-08"), at(2026, 11, 8, 20, 30))


def test_new_local_day_resets():
    # 00:30 Moscow on 11-09: a new day, but before 20:00
    assert not is_send_due(mk(last_run_date="2026-11-08"), at(2026, 11, 8, 21, 30))
    assert is_send_due(mk(last_run_date="2026-11-08"), at(2026, 11, 9, 17, 0))


def test_paused_never_due():
    assert not is_send_due(mk(paused=True), at(2026, 11, 8, 18, 0))


def test_offset_timezone():
    assert is_send_due(mk(timezone="Etc/GMT+5"), at(2026, 11, 9, 1, 0))  # 20:00 at UTC-5
    assert not is_send_due(mk(timezone="Etc/GMT+5"), at(2026, 11, 9, 0, 59))


def test_dst_new_york():
    u = mk(timezone="America/New_York", send_time="09:00")
    assert is_send_due(u, at(2026, 7, 1, 13, 0))       # EDT, UTC-4
    assert not is_send_due(u, at(2026, 12, 1, 13, 0))  # EST, UTC-5 → 08:00 local
    assert is_send_due(u, at(2026, 12, 1, 14, 0))


def test_health_window():
    u = mk()  # send 20:00 Moscow = 17:00 UTC → window 14:00–17:00 UTC
    assert not is_health_check_due(u, at(2026, 11, 8, 13, 59))
    assert is_health_check_due(u, at(2026, 11, 8, 14, 0))
    assert is_health_check_due(u, at(2026, 11, 8, 16, 59))
    assert not is_health_check_due(u, at(2026, 11, 8, 17, 0))
    assert not is_health_check_due(mk(last_health_date="2026-11-08"), at(2026, 11, 8, 15, 0))


def test_health_check_not_for_unrunnable():
    assert not is_health_check_due(mk(paused=True), at(2026, 11, 8, 15, 0))


def test_health_window_crosses_midnight():
    # today's (11-08) 01:00 run is done; next send is 11-09 01:00 Moscow = 11-08 22:00 UTC
    u = mk(send_time="01:00", last_run_date="2026-11-08")
    assert is_health_check_due(u, at(2026, 11, 8, 19, 30))  # 22:30 Moscow on 11-08
    assert not is_health_check_due(
        mk(send_time="01:00", last_run_date="2026-11-08", last_health_date="2026-11-09"),
        at(2026, 11, 8, 19, 30),
    )


def test_health_after_todays_run_targets_tomorrow():
    # run done at 20:00 today; tomorrow's window starts 17:00 Moscow tomorrow
    u = mk(last_run_date="2026-11-08", last_health_date="2026-11-08")
    assert not is_health_check_due(u, at(2026, 11, 8, 18, 0))
    assert is_health_check_due(u, at(2026, 11, 9, 14, 0))


def test_health_check_target():
    from streakbot.scheduling import health_check_target
    assert health_check_target(mk(), at(2026, 11, 8, 15, 0)) == date(2026, 11, 8)
    assert health_check_target(mk(last_run_date="2026-11-08"), at(2026, 11, 8, 18, 0)) == date(2026, 11, 9)


def test_next_send_at_today():
    # 19:00 Moscow, not sent yet → today 20:00 Moscow = 17:00 UTC
    assert next_send_at(mk(), at(2026, 11, 8, 16, 0)) == at(2026, 11, 8, 17, 0)
    # overdue but not yet run (catch-up) → still today's time
    assert next_send_at(mk(), at(2026, 11, 8, 18, 0)) == at(2026, 11, 8, 17, 0)


def test_next_send_at_tomorrow_after_run():
    assert next_send_at(mk(last_run_date="2026-11-08"), at(2026, 11, 8, 18, 0)) == at(2026, 11, 9, 17, 0)


def test_next_send_at_none_when_paused():
    assert next_send_at(mk(paused=True), at(2026, 11, 8, 16, 0)) is None
    assert next_send_at(mk(send_time=None), at(2026, 11, 8, 16, 0)) is None

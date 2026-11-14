import sqlite3

import pytest

from streakbot.db import Recipient, Repo


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "db.sqlite")


def new_bot(repo, username="owner.acct"):
    return repo.create_bot(username, "useast5", "Europe/Moscow", b"sealed")


def test_create_and_get_bot(repo):
    b = new_bot(repo)
    assert repo.get_bot(b.id) == b
    assert repo.get_bot_by_username("owner.acct") == b
    assert b.message == "🔥"
    assert b.region == "useast5" and b.timezone == "Europe/Moscow"
    assert not b.paused and b.send_time is None


def test_missing_bot_is_none(repo):
    assert repo.get_bot(999) is None
    assert repo.get_bot_by_username("nobody") is None


def test_duplicate_username_rejected(repo):
    new_bot(repo)
    with pytest.raises(sqlite3.IntegrityError):
        new_bot(repo)


def test_list_bots_ordered(repo):
    new_bot(repo, "b.one")
    new_bot(repo, "a.two")
    assert [b.tiktok_username for b in repo.list_bots()] == ["b.one", "a.two"]


def test_update_fields_targeted(repo):
    b = new_bot(repo)
    repo.update_fields(b.id, paused=True)
    repo.update_fields(b.id, last_run_ok=True, last_run_date="2026-11-08", last_run_summary="Sent 1/1")
    got = repo.get_bot(b.id)
    assert got.paused is True and got.last_run_ok is True
    assert got.last_run_date == "2026-11-08" and got.last_run_summary == "Sent 1/1"


@pytest.mark.parametrize("bad", [{"id": 5}, {"nope": 1}, {}])
def test_update_fields_rejects(repo, bad):
    b = new_bot(repo)
    with pytest.raises(ValueError):
        repo.update_fields(b.id, **bad)


def test_cookies(repo):
    b = new_bot(repo)
    assert repo.get_cookies_blob(b.id) == b"sealed"
    repo.replace_cookies(b.id, b"other")
    assert repo.get_cookies_blob(b.id) == b"other"
    assert repo.replace_cookies(b.id, b"new") is True
    assert repo.get_cookies_blob(b.id) == b"new"


def test_replace_cookies_after_delete(repo):
    b = new_bot(repo)
    repo.delete_bot(b.id)
    assert repo.replace_cookies(b.id, b"new") is False
    assert repo.get_bot(b.id) is None


def test_recipients_order_and_cascade(repo):
    b = new_bot(repo)
    repo.set_recipients(b.id, [Recipient("b", "B"), Recipient("a", "A")])
    assert repo.get_recipients(b.id) == [Recipient("b", "B"), Recipient("a", "A")]
    repo.set_recipients(b.id, [Recipient("c", "C")])
    assert repo.get_recipients(b.id) == [Recipient("c", "C")]
    repo.delete_bot(b.id)
    assert repo.get_recipients(b.id) == []


def test_data_survives_reopen(tmp_path):
    Repo(tmp_path / "db.sqlite").create_bot("owner.acct", "useast5", "UTC", b"x")
    assert Repo(tmp_path / "db.sqlite").get_bot_by_username("owner.acct") is not None

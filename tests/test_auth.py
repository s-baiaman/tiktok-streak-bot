import os
import stat

import pytest

from streakbot.auth import (
    LoginThrottle,
    SessionSigner,
    hash_password,
    load_or_create_session_key,
    verify_password,
)

KEY = b"k" * 32


def test_hash_and_verify():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$16384$8$1$")
    assert "correct horse" not in h
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)
    assert hash_password("correct horse") != h  # salted


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$x$8$1$a$b", "md5$1$2$3$4$5", "scrypt$16384$8$1$!!$!!"])
def test_verify_malformed(stored):
    assert verify_password("anything", stored) is False


def test_signer_roundtrip():
    clock = {"t": 1_000_000.0}
    s = SessionSigner(KEY, "hash1", clock=lambda: clock["t"])
    token = s.issue()
    assert s.valid(token)
    clock["t"] += SessionSigner.MAX_AGE - 1
    assert s.valid(token)
    clock["t"] += 2
    assert not s.valid(token)


def test_signer_rejects_tampering_and_other_password():
    s = SessionSigner(KEY, "hash1", clock=lambda: 1_000_000.0)
    token = s.issue()
    issued, sig = token.split(".")
    assert not s.valid(f"{int(issued) + 100}.{sig}")
    assert not s.valid(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not SessionSigner(KEY, "hash2", clock=lambda: 1_000_000.0).valid(token)
    assert not SessionSigner(b"x" * 32, "hash1", clock=lambda: 1_000_000.0).valid(token)


@pytest.mark.parametrize("token", [None, "", "nodot", "a.b", ".", "123."])
def test_signer_garbage(token):
    assert not SessionSigner(KEY, "h").valid(token)


def test_signer_rejects_future_tokens():
    s_future = SessionSigner(KEY, "h", clock=lambda: 2_000_000.0)
    assert not SessionSigner(KEY, "h", clock=lambda: 1_000_000.0).valid(s_future.issue())


def test_session_key_created_private(tmp_path):
    path = tmp_path / "session.key"
    key = load_or_create_session_key(path)
    assert len(key) == 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_or_create_session_key(path) == key


def test_throttle():
    clock = {"t": 0.0}
    th = LoginThrottle(limit=10, window=900, clock=lambda: clock["t"])
    for _ in range(10):
        assert th.allowed("1.2.3.4")
        th.failed("1.2.3.4")
    assert not th.allowed("1.2.3.4")
    assert th.allowed("5.6.7.8")
    clock["t"] += 901
    assert th.allowed("1.2.3.4")


def test_throttle_reset():
    th = LoginThrottle(limit=2, window=900, clock=lambda: 0.0)
    th.failed("a")
    th.failed("a")
    assert not th.allowed("a")
    th.reset("a")
    assert th.allowed("a")

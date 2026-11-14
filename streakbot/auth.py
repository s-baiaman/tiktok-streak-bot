"""Panel password hashing, signed session cookies and login throttling."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from .crypto import load_or_create_secret

_N, _R, _P = 16384, 8, 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(_scrypt(password, salt, _N, _R, _P))}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if n > 2**20 or r > 32 or p > 16:
            return False
        expected = base64.urlsafe_b64decode(digest.encode())
        actual = _scrypt(password, base64.urlsafe_b64decode(salt.encode()), n, r, p)
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


def load_or_create_session_key(path: Path) -> bytes:
    return load_or_create_secret(path, lambda: secrets.token_bytes(32))


class SessionSigner:
    """Tokens look like `<issued unix time>.<hmac>`; the hmac also covers the password hash,
    so changing the password logs every browser out."""

    MAX_AGE = 30 * 86400

    def __init__(self, key: bytes, password_hash: str, clock: Callable[[], float] = time.time):
        self._key = key
        self._password_hash = password_hash
        self._clock = clock

    def _sign(self, issued: str) -> str:
        return hmac.new(self._key, f"{issued}|{self._password_hash}".encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        issued = str(int(self._clock()))
        return f"{issued}.{self._sign(issued)}"

    def valid(self, token: str | None) -> bool:
        issued, _, signature = (token or "").partition(".")
        if not issued.isdigit() or not hmac.compare_digest(signature, self._sign(issued)):
            return False
        age = self._clock() - int(issued)
        return 0 <= age <= self.MAX_AGE


class LoginThrottle:
    def __init__(self, limit: int = 10, window: float = 900, clock: Callable[[], float] = time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, client: str) -> deque[float]:
        q = self._failures[client]
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def allowed(self, client: str) -> bool:
        return len(self._prune(client)) < self._limit

    def failed(self, client: str) -> None:
        self._prune(client).append(self._clock())

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)

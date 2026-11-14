"""Encryption at rest for TikTok session cookies.

Protects backups and copied disks. It does not protect against the host,
who holds the key (the README says so).
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create_secret(path: Path, generate: Callable[[], bytes]) -> bytes:
    """Read a secret file, or create it (owner-only, 0600) with `generate()` if it doesn't exist yet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    secret = generate()
    with os.fdopen(fd, "wb") as f:
        f.write(secret)
    return secret


def load_or_create_key(path: Path) -> bytes:
    return load_or_create_secret(path, Fernet.generate_key).strip()


class CookieBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def seal(self, cookies: list[dict]) -> bytes:
        return self._fernet.encrypt(json.dumps(cookies).encode())

    def open(self, blob: bytes) -> list[dict]:
        return json.loads(self._fernet.decrypt(blob))

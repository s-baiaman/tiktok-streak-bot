"""SQLite storage for bots, their recipients and their sealed cookies."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, fields
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tiktok_username TEXT NOT NULL UNIQUE,
    region TEXT,
    message TEXT NOT NULL DEFAULT '🔥',
    send_time TEXT,
    timezone TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    last_run_date TEXT,
    last_run_ok INTEGER,
    last_run_at TEXT,
    last_run_summary TEXT,
    session_ok INTEGER,
    last_health_date TEXT,
    session_expires_at TEXT,
    cookies_enc BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS recipients (
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    PRIMARY KEY (bot_id, username)
);
"""


@dataclass
class Bot:
    id: int
    tiktok_username: str
    region: str | None = None
    message: str = "🔥"
    send_time: str | None = None
    timezone: str | None = None
    paused: bool = False
    last_run_date: str | None = None
    last_run_ok: bool | None = None
    last_run_at: str | None = None
    last_run_summary: str | None = None
    session_ok: bool | None = None
    last_health_date: str | None = None
    session_expires_at: str | None = None


@dataclass
class Recipient:
    username: str
    display_name: str


_BOT_FIELDS = [f.name for f in fields(Bot)]
_BOOL_FIELDS = {"paused", "last_run_ok", "session_ok"}
_MUTABLE_FIELDS = set(_BOT_FIELDS) - {"id", "tiktok_username"}
_SELECT = f"SELECT {', '.join(_BOT_FIELDS)} FROM bots"


def _row_to_bot(row: sqlite3.Row) -> Bot:
    values = {}
    for name in _BOT_FIELDS:
        v = row[name]
        if name in _BOOL_FIELDS and v is not None:
            v = bool(v)
        values[name] = v
    return Bot(**values)


class Repo:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)

    def _one(self, where: str, params: tuple) -> Bot | None:
        with self._lock:
            row = self._conn.execute(f"{_SELECT} {where}", params).fetchone()
        return _row_to_bot(row) if row else None

    def create_bot(
        self, tiktok_username: str, region: str | None, timezone: str, cookies_blob: bytes,
        session_ok: bool | None = None,
    ) -> Bot:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO bots (tiktok_username, region, timezone, cookies_enc, session_ok) VALUES (?, ?, ?, ?, ?)",
                (tiktok_username, region, timezone, cookies_blob, session_ok),
            )
            bot_id = cur.lastrowid
        return self.get_bot(bot_id)

    def get_bot(self, bot_id: int) -> Bot | None:
        return self._one("WHERE id = ?", (bot_id,))

    def get_bot_by_username(self, username: str) -> Bot | None:
        return self._one("WHERE tiktok_username = ?", (username,))

    def list_bots(self) -> list[Bot]:
        with self._lock:
            rows = self._conn.execute(f"{_SELECT} ORDER BY id").fetchall()
        return [_row_to_bot(r) for r in rows]

    def update_fields(self, bot_id: int, **values) -> None:
        """Write only the named columns, so concurrent changes to other columns survive."""
        unknown = set(values) - _MUTABLE_FIELDS
        if unknown or not values:
            raise ValueError(f"cannot update fields: {sorted(unknown) or 'none given'}")
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE bots SET {assignments} WHERE id = ?", (*values.values(), bot_id))

    def get_cookies_blob(self, bot_id: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute("SELECT cookies_enc FROM bots WHERE id = ?", (bot_id,)).fetchone()
        return row["cookies_enc"] if row else None

    def replace_cookies(self, bot_id: int, blob: bytes) -> bool:
        """Replace stored cookies; False if the bot was deleted meanwhile."""
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE bots SET cookies_enc = ? WHERE id = ?", (blob, bot_id))
            return cur.rowcount == 1

    def delete_bot(self, bot_id: int) -> None:
        with self._lock, self._conn:  # recipients go with it (ON DELETE CASCADE)
            self._conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))

    def get_recipients(self, bot_id: int) -> list[Recipient]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, display_name FROM recipients WHERE bot_id = ? ORDER BY position", (bot_id,)
            ).fetchall()
        return [Recipient(r["username"], r["display_name"]) for r in rows]

    def set_recipients(self, bot_id: int, recipients: list[Recipient]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM recipients WHERE bot_id = ?", (bot_id,))
            self._conn.executemany(
                "INSERT INTO recipients (bot_id, position, username, display_name) VALUES (?, ?, ?, ?)",
                [(bot_id, i, r.username, r.display_name) for i, r in enumerate(recipients)],
            )

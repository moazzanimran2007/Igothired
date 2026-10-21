"""Local SQLite store. This is the source of truth; the Google Sheet is a view of it."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS founders (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    headline      TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    signal        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    stage         TEXT NOT NULL DEFAULT 'new',
    fit_score     INTEGER,
    fit_reason    TEXT NOT NULL DEFAULT '',
    brief         TEXT NOT NULL DEFAULT '',
    profile_text  TEXT NOT NULL DEFAULT '',
    transcript    TEXT NOT NULL DEFAULT '',
    connection    TEXT NOT NULL DEFAULT 'none',
    invited_at    TEXT,
    thread_id     TEXT,
    inbox_snippet TEXT NOT NULL DEFAULT '',
    needs_you     TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    founder_id  INTEGER NOT NULL REFERENCES founders(id),
    direction   TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL,
    body_hash   TEXT NOT NULL,
    status      TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    warnings    TEXT NOT NULL DEFAULT '[]',
    feedback    TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    send_after  TEXT,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS inbound_once ON messages (founder_id, body_hash) WHERE direction = 'in';
CREATE INDEX IF NOT EXISTS messages_by_status ON messages (status, kind);

CREATE TABLE IF NOT EXISTS actions (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    ok      INTEGER NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS actions_by_kind ON actions (kind, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FOUNDER_COLUMNS = {
    "name", "headline", "company", "signal", "source", "stage", "fit_score", "fit_reason",
    "brief", "profile_text", "transcript", "connection", "invited_at", "thread_id",
    "inbox_snippet", "needs_you", "error",
}
MESSAGE_COLUMNS = {
    "body", "status", "category", "warnings", "feedback", "attempts", "send_after", "error", "sent_at",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).casefold().encode()).hexdigest()[:20]


def warnings_of(row: sqlite3.Row) -> list[str]:
    try:
        return json.loads(row["warnings"] or "[]")
    except json.JSONDecodeError:
        return [row["warnings"]]


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        os.chmod(path, 0o600)

    def close(self) -> None:
        self.db.close()

    # -- key/value ---------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def unset(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- pausing -----------------------------------------------------------

    def pause(self, reason: str, until: datetime | None = None) -> None:
        self.set("pause_reason", reason)
        if until:
            self.set("pause_until", stamp(until))
        else:
            self.unset("pause_until")

    def resume(self) -> None:
        self.unset("pause_reason")
        self.unset("pause_until")

    def pause_state(self) -> str | None:
        """Why LinkedIn is off-limits right now, or None if it isn't."""
        if self.get("sheet_paused") == "1":
            return "the Paused box is ticked in the sheet"
        reason = self.get("pause_reason")
        if not reason:
            return None
        until = self.get("pause_until")
        if until and parse_stamp(until) <= utcnow():
            self.resume()
            return None
        return reason

    # -- founders ----------------------------------------------------------

    def add_founder(self, username: str, **fields: Any) -> int | None:
        """Insert someone new. Returns None if we already know them."""
        cols = {k: v for k, v in fields.items() if k in FOUNDER_COLUMNS}
        now = stamp()
        names = ["username", "created_at", "updated_at", *cols]
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO founders ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            (username, now, now, *cols.values()),
        )
        return cur.lastrowid if cur.rowcount else None

    def founder(self, founder_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM founders WHERE id = ?", (founder_id,)).fetchone()

    def founder_by_username(self, username: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM founders WHERE username = ?", (username,)).fetchone()

    def founders(self, *stages: str, limit: int | None = None, order: str = "updated_at DESC") -> list[sqlite3.Row]:
        assert order in {"updated_at DESC", "created_at", "fit_score DESC"}
        sql, args = "SELECT * FROM founders", []
        if stages:
            sql += f" WHERE stage IN ({', '.join('?' * len(stages))})"
            args += stages
        sql += f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.execute(sql, args).fetchall()

    def update_founder(self, founder_id: int, **fields: Any) -> None:
        unknown = set(fields) - FOUNDER_COLUMNS
        if unknown:
            raise ValueError(f"unknown founder fields: {unknown}")
        fields["updated_at"] = stamp()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE founders SET {sets} WHERE id = ?", (*fields.values(), founder_id))

    def stage_counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT stage, COUNT(*) AS n FROM founders GROUP BY stage").fetchall()
        return {r["stage"]: r["n"] for r in rows}

    # -- messages ----------------------------------------------------------

    def add_message(self, founder_id: int, direction: str, kind: str, body: str, status: str, **fields: Any) -> int | None:
        cols = {k: v for k, v in fields.items() if k in MESSAGE_COLUMNS}
        if isinstance(cols.get("warnings"), list):
            cols["warnings"] = json.dumps(cols["warnings"])
        names = ["founder_id", "direction", "kind", "body", "body_hash", "status", "created_at", *cols]
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO messages ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            (founder_id, direction, kind, body, text_hash(body), status, stamp(), *cols.values()),
        )
        return cur.lastrowid if cur.rowcount else None

    def message(self, message_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()

    def messages(
        self,
        founder_id: int | None = None,
        *,
        kinds: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
    ) -> list[sqlite3.Row]:
        sql, args, where = "SELECT * FROM messages", [], []
        if founder_id is not None:
            where.append("founder_id = ?")
            args.append(founder_id)
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args += kinds
        if statuses:
            where.append(f"status IN ({', '.join('?' * len(statuses))})")
            args += statuses
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def latest(self, founder_id: int, kind: str, statuses: tuple[str, ...]) -> sqlite3.Row | None:
        rows = self.messages(founder_id, kinds=(kind,), statuses=statuses)
        return rows[-1] if rows else None

    def update_message(self, message_id: int, **fields: Any) -> None:
        unknown = set(fields) - MESSAGE_COLUMNS
        if unknown:
            raise ValueError(f"unknown message fields: {unknown}")
        if isinstance(fields.get("warnings"), list):
            fields["warnings"] = json.dumps(fields["warnings"])
        if "body" in fields:
            fields["body_hash"] = text_hash(fields["body"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE messages SET {sets} WHERE id = ?", (*fields.values(), message_id))

    def supersede(self, founder_id: int, kinds: tuple[str, ...]) -> None:
        """Retire unsent drafts before writing new ones, so only one version is ever live."""
        self.db.execute(
            f"UPDATE messages SET status = 'superseded' WHERE founder_id = ? AND direction = 'out' "
            f"AND status IN ('review', 'redraft', 'approved') AND kind IN ({', '.join('?' * len(kinds))})",
            (founder_id, *kinds),
        )

    def activity_since(self, since: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT m.*, f.name AS founder_name FROM messages m JOIN founders f ON f.id = m.founder_id
            WHERE (m.direction = 'out' AND m.sent_at > ?)
               OR (m.direction = 'in' AND m.created_at > ?)
            ORDER BY COALESCE(m.sent_at, m.created_at)
            """,
            (since, since),
        ).fetchall()

    # -- action log (also what the rate limits count) ----------------------

    def log_action(self, kind: str, target: str, ok: bool, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO actions (ts, kind, target, ok, detail) VALUES (?, ?, ?, ?, ?)",
            (stamp(), kind, target, int(ok), detail[:500]),
        )

    def count_actions(self, kind: str, within: timedelta) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM actions WHERE kind = ? AND ok = 1 AND ts > ?",
            (kind, stamp(utcnow() - within)),
        ).fetchone()
        return row["n"]

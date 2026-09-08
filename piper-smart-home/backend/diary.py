"""PIPER backend — the diary (SQLite event store).

Section 3.4.6 of the report. Every appliance change, rule action and notable
conversation turn lands here, which is what makes "did I leave the stove on?"
answerable hours later.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    event   TEXT    NOT NULL,
    value   TEXT,
    source  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
"""


class Diary:
    def __init__(self, path: str | None = None):
        self.path = path or config.DB_PATH
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def log(self, event: str, value: str = "", source: str = "system"):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, day, event, value, source) VALUES (?,?,?,?,?)",
                (now, datetime.fromtimestamp(now).strftime("%Y-%m-%d"), event, value, source),
            )
            self._conn.commit()

    def today(self, limit: int = 40) -> list[tuple]:
        day = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, event, value, source FROM events WHERE day=? "
                "ORDER BY ts DESC LIMIT ?", (day, limit))
            return cur.fetchall()

    def search(self, keyword: str, days: int = 2, limit: int = 10) -> list[tuple]:
        since = int(time.time()) - days * 86400
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, event, value, source FROM events "
                "WHERE ts >= ? AND (event LIKE ? OR value LIKE ?) "
                "ORDER BY ts DESC LIMIT ?",
                (since, f"%{keyword}%", f"%{keyword}%", limit))
            return cur.fetchall()

    def last(self, event_like: str) -> tuple | None:
        rows = self.search(event_like, days=7, limit=1)
        return rows[0] if rows else None

    @staticmethod
    def as_lines(rows) -> list[str]:
        """Format rows for the Gemini prompt: '08:00 — medicine: taken'."""
        return [
            f"{datetime.fromtimestamp(ts).strftime('%H:%M')} — {event}: {value}"
            for ts, event, value, _ in reversed(rows)
        ]

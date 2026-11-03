"""PIPER backend — the diary (SQLite event store).

Section 3.4.6 of the report. Every appliance change, rule action and notable
conversation turn lands here, which is what makes "did I leave the stove on?"
answerable hours later.

How it is organised (steps 1-5 of the diary work):

  Step 2  the table      one row = one thing that happened
  Step 3  writing        log()             stamps the time itself
  Step 4  reading        events_between()  all events of one kind in a time range
                         last_event()      newest event for one exact device/item
  Step 5  time windows   window_bounds()   "today" -> (start, end)

The older helpers today(), search(), last() and as_lines() are kept unchanged
so pipeline.py, command_bridge.py, rules.py and the tests keep working.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# Step 2 — the table
# ---------------------------------------------------------------------------
# ts     when it happened, as Unix seconds (an integer sorts and compares
#        correctly, the same reason the text format YYYY-MM-DD HH:MM:SS works)
# day    the local date as text, kept for the existing today() helper
# kind   what type of event: one of KINDS below
# event  which thing: a device id from config.DEVICES ("stove"), or a short
#        label from the conversation ("medicine")
# value  what happened ("off (ok)", "taken")
# source who caused it ("voice", "rule", "conversation")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    event   TEXT    NOT NULL,
    value   TEXT,
    source  TEXT,
    kind    TEXT    NOT NULL DEFAULT 'note'
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
"""

# The fixed menu of kinds. Step 7 checks Gemini's choice against this.
KINDS = ("device", "medication", "alert", "note")

# The fixed menu of time windows. Step 7 checks Gemini's choice against this.
WINDOWS = ("today", "yesterday", "this_morning", "this_afternoon", "this_evening")

_MEDICATION_WORDS = ("medic", "pill", "tablet", "dose", "capsule")


def _resolve_path(path: str | None) -> str:
    """Keep the database next to this file, whatever folder PIPER is started from.

    An absolute path (the tests set PIPER_DB to a temp file) is used as given.
    ":memory:" is passed straight through.
    """
    p = path or config.DB_PATH
    if p == ":memory:" or Path(p).is_absolute():
        return str(p)
    return str(Path(__file__).resolve().parent / p)


def guess_kind(event: str, value: str = "", source: str = "") -> str:
    """Decide the kind when the caller doesn't say. Keeps old log() calls working."""
    e, v = (event or "").lower(), (value or "").lower()
    if any(w in e or w in v for w in _MEDICATION_WORDS):
        return "medication"
    if source == "rule" and ("cut off" in v or "warning" in v):
        return "alert"
    if event in config.DEVICES:
        return "device"
    return "note"


class Diary:
    def __init__(self, path: str | None = None):
        self.path = _resolve_path(path)
        self._lock = threading.Lock()        # MQTT, rules and Flask threads all write
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._migrate()

    def _migrate(self):
        """Create the table, or add the kind column to a diary made before it existed."""
        with self._lock:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(events)")]
            if cols and "kind" not in cols:
                self._conn.execute(
                    "ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT 'note'")
                rows = self._conn.execute(
                    "SELECT id, event, value, source FROM events").fetchall()
                for row_id, event, value, source in rows:
                    self._conn.execute("UPDATE events SET kind=? WHERE id=?",
                                       (guess_kind(event, value or "", source or ""), row_id))
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts)")
            self._conn.commit()

    # -----------------------------------------------------------------------
    # Step 3 — writing
    # -----------------------------------------------------------------------

    def log(self, event: str, value: str = "", source: str = "system",
            kind: str | None = None, ts: int | None = None):
        """Write one line in the diary.

        The time is filled in here, so callers can't get it wrong. `ts` is only
        for tests that need an event "yesterday".
        """
        if kind is None:
            kind = guess_kind(event, value, source)
        if kind not in KINDS:
            raise ValueError(f"unknown diary kind {kind!r}")
        now = int(time.time()) if ts is None else int(ts)
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, day, event, value, source, kind) "
                "VALUES (?,?,?,?,?,?)",
                (now, datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
                 event, value, source, kind),
            )
            self._conn.commit()

    # -----------------------------------------------------------------------
    # Step 4 — reading
    # All readers return rows as (ts, event, value, source), the same shape
    # as the older helpers, so as_lines() works on every result.
    # -----------------------------------------------------------------------

    def events_between(self, kind: str, start: int, end: int) -> list[tuple]:
        """All events of one kind from start (included) to end (not included), oldest first."""
        with self._lock:
            return self._conn.execute(
                "SELECT ts, event, value, source FROM events "
                "WHERE kind = ? AND ts >= ? AND ts < ? "
                "ORDER BY ts, id",
                (kind, start, end)).fetchall()

    def last_event(self, event: str, window: str | None = None) -> tuple | None:
        """Newest event for one exact device or item, or None if there isn't one.

        Exact match, so "living_light" never picks up "bedroom_light".
        With a window ("today"), only that window is searched.
        """
        sql = "SELECT ts, event, value, source FROM events WHERE event = ?"
        params: list = [event]
        if window is not None:
            bounds = window_bounds(window)
            if bounds is None:
                return None
            sql += " AND ts >= ? AND ts < ?"
            params += list(bounds)
        sql += " ORDER BY ts DESC, id DESC LIMIT 1"
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def events_in(self, kind: str, window: str) -> list[tuple] | None:
        """events_between() with a window word. None means the word was not allowed."""
        bounds = window_bounds(window)
        if bounds is None:
            return None
        return self.events_between(kind, *bounds)

    # -----------------------------------------------------------------------
    # Older helpers, unchanged — used by pipeline.py, main.py and the tests
    # -----------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Step 5 — time windows
# ---------------------------------------------------------------------------

def window_bounds(window: str, now: datetime | None = None) -> tuple[int, int] | None:
    """Turn a word like "today" into (start, end) Unix seconds, using the local clock.

    The end is never included, so an event at exactly midnight belongs to the
    new day only. Unknown words return None: refuse instead of guessing.
    `now` is only for tests.
    """
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if window == "today":
        start, end = midnight, midnight + timedelta(days=1)
    elif window == "yesterday":
        start, end = midnight - timedelta(days=1), midnight
    elif window == "this_morning":
        start, end = midnight, midnight + timedelta(hours=12)
    elif window == "this_afternoon":
        start, end = midnight + timedelta(hours=12), midnight + timedelta(hours=18)
    elif window == "this_evening":
        start, end = midnight + timedelta(hours=18), midnight + timedelta(days=1)
    else:
        return None

    return int(start.timestamp()), int(end.timestamp())


def clock(ts: int) -> str:
    """Unix seconds -> '08:15', for spoken answers (step 8)."""
    return datetime.fromtimestamp(ts).strftime("%H:%M")

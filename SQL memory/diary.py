import sqlite3
from datetime import datetime, date, timedelta

DB_FILE = "diary_practice.db"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Open the database file (created automatically if it doesn't exist)
db = sqlite3.connect(DB_FILE)

# Make the table, but only if it isn't there already
db.execute("""
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    what_time     TEXT NOT NULL,
    what_type     TEXT NOT NULL,
    which_thing   TEXT NOT NULL,
    what_happened TEXT NOT NULL,
    who_caused_it TEXT NOT NULL
)
""")
db.commit()


def log_event(what_type, which_thing, what_happened, who_caused_it):
    """Write one line in the diary, stamped with the current time."""
    now = datetime.now().strftime(TIME_FORMAT)
    db.execute(
        "INSERT INTO events (what_time, what_type, which_thing, what_happened, who_caused_it) "
        "VALUES (?, ?, ?, ?, ?)",
        (now, what_type, which_thing, what_happened, who_caused_it)
    )
    db.commit()


def all_events():
    """Return every row, oldest first. For checking only."""
    return db.execute("SELECT * FROM events ORDER BY what_time").fetchall()

def events_between(what_type, start, end):
    """All events of one type from start (included) up to end (not included), oldest first."""
    return db.execute(
        "SELECT * FROM events "
        "WHERE what_type = ? AND what_time >= ? AND what_time < ? "
        "ORDER BY what_time",
        (what_type, start, end)
    ).fetchall()


def last_event(which_thing):
    """The most recent event for one device or item, or None if there isn't one."""
    return db.execute(
        "SELECT * FROM events "
        "WHERE which_thing = ? "
        "ORDER BY what_time DESC "
        "LIMIT 1",
        (which_thing,)
    ).fetchone()


def window_bounds(window):
    """Turn a word like 'today' into (start, end) times. Returns None for unknown words."""
    midnight_today = datetime.combine(date.today(), datetime.min.time())

    if window == "today":
        start = midnight_today
        end = midnight_today + timedelta(days=1)
    elif window == "yesterday":
        start = midnight_today - timedelta(days=1)
        end = midnight_today
    elif window == "this_morning":
        start = midnight_today
        end = midnight_today + timedelta(hours=12)
    else:
        return None   # unknown word: refuse instead of guessing

    return start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)


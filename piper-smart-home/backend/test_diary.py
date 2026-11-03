"""Tests for the diary (steps 1-5). No broker, no ESP32, no Gemini needed.

    python test_diary.py

Every test uses its own throwaway database, so your real piper_diary.db is
never touched.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diary import Diary, KINDS, WINDOWS, clock, guess_kind, window_bounds  # noqa: E402


def ts_at(day: datetime, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(day.replace(hour=hour, minute=minute, second=second,
                           microsecond=0).timestamp())


class DiaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "diary_test.db")
        self.diary = Diary(self.db_path)
        self.today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.yesterday = self.today - timedelta(days=1)

    def tearDown(self):
        self.diary._conn.close()
        self.tmp.cleanup()

    # -- step 3: writing ----------------------------------------------------

    def test_01_log_fills_time_and_kind(self):
        self.diary.log("stove", "off (ok)", source="voice")
        row = self.diary._conn.execute(
            "SELECT ts, day, event, value, source, kind FROM events").fetchone()
        self.assertAlmostEqual(row[0], int(datetime.now().timestamp()), delta=5)
        self.assertEqual(row[1], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(row[2:], ("stove", "off (ok)", "voice", "device"))

    def test_02_kind_guessing(self):
        self.assertEqual(guess_kind("living_light", "on (ok)", "voice"), "device")
        self.assertEqual(guess_kind("medicine", "taken", "conversation"), "medication")
        self.assertEqual(guess_kind("morning pills", "taken", "conversation"), "medication")
        self.assertEqual(guess_kind("stove", "cut off automatically after 20 min", "rule"),
                         "alert")
        self.assertEqual(guess_kind("visitor", "daughter came", "conversation"), "note")

    def test_03_unknown_kind_refused(self):
        with self.assertRaises(ValueError):
            self.diary.log("stove", "off", kind="whatever")

    # -- step 4: reading ----------------------------------------------------

    def test_10_events_between_only_that_kind_and_range(self):
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.today, 8, 15))
        self.diary.log("fan", "set (ok)", "voice", ts=ts_at(self.today, 8, 30))
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.yesterday, 8, 10))
        start, end = window_bounds("today")
        rows = self.diary.events_between("medication", start, end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:3], ("medicine", "taken"))

    def test_11_empty_diary_is_empty_list(self):
        self.assertEqual(self.diary.events_in("medication", "today"), [])

    def test_12_last_event_exact_match(self):
        self.diary.log("living_light", "on (ok)", "voice", ts=ts_at(self.today, 7))
        self.diary.log("bedroom_light", "off (ok)", "voice", ts=ts_at(self.today, 9))
        row = self.diary.last_event("living_light")
        self.assertEqual(row[1:3], ("living_light", "on (ok)"))   # not the bedroom light

    def test_13_last_event_newest_and_none(self):
        self.diary.log("stove", "off (ok)", "voice", ts=ts_at(self.today, 7))
        self.diary.log("stove", "warning, on 15 min unattended", "rule", ts=ts_at(self.today, 12))
        self.assertEqual(self.diary.last_event("stove")[2], "warning, on 15 min unattended")
        self.assertIsNone(self.diary.last_event("hallway_light"))

    def test_14_last_event_with_window(self):
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.yesterday, 8))
        # Taken yesterday must NOT answer "today" — that is how double doses happen.
        self.assertIsNone(self.diary.last_event("medicine", window="today"))
        self.assertIsNotNone(self.diary.last_event("medicine", window="yesterday"))
        self.assertIsNone(self.diary.last_event("medicine", window="last_week"))

    # -- step 5: time windows ----------------------------------------------

    def test_20_window_bounds(self):
        now = datetime(2026, 11, 2, 19, 30)
        s, e = window_bounds("today", now)
        self.assertEqual(datetime.fromtimestamp(s), datetime(2026, 11, 2))
        self.assertEqual(datetime.fromtimestamp(e), datetime(2026, 11, 3))
        s, e = window_bounds("yesterday", now)
        self.assertEqual(datetime.fromtimestamp(s), datetime(2026, 11, 1))
        s, e = window_bounds("this_morning", now)
        self.assertEqual(datetime.fromtimestamp(e), datetime(2026, 11, 2, 12))

    def test_21_month_and_year_boundaries(self):
        s, _ = window_bounds("yesterday", datetime(2026, 11, 1, 9))
        self.assertEqual(datetime.fromtimestamp(s), datetime(2026, 10, 31))
        s, _ = window_bounds("yesterday", datetime(2027, 1, 1, 9))
        self.assertEqual(datetime.fromtimestamp(s), datetime(2026, 12, 31))

    def test_22_unknown_window_refused(self):
        for word in ("last_week", "", "today; DROP TABLE events", None):
            self.assertIsNone(window_bounds(word))
            self.assertIsNone(self.diary.events_in("medication", word))

    def test_23_midnight_belongs_to_new_day_only(self):
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.today, 0, 0, 0))
        self.assertEqual(len(self.diary.events_in("medication", "today")), 1)
        self.assertEqual(len(self.diary.events_in("medication", "yesterday")), 0)

    def test_24_menus_match_window_bounds(self):
        for w in WINDOWS:
            self.assertIsNotNone(window_bounds(w), w)
        self.assertIn("medication", KINDS)

    # -- compatibility with the rest of PIPER --------------------------------

    def test_30_old_helpers_still_work(self):
        self.diary.log("medicine", "taken", source="conversation")
        self.assertEqual(self.diary.search("medicine")[0][1], "medicine")
        self.assertEqual(self.diary.last("medicine")[1], "medicine")
        lines = Diary.as_lines(self.diary.today(limit=5))
        self.assertTrue(lines and all("—" in l for l in lines))
        # New readers return the same row shape, so as_lines works on them too.
        self.assertTrue(Diary.as_lines(self.diary.events_in("medication", "today")))
        self.assertRegex(clock(self.diary.last_event("medicine")[0]), r"^\d\d:\d\d$")

    def test_31_upgrades_an_old_diary(self):
        old = str(Path(self.tmp.name) / "old.db")
        c = sqlite3.connect(old)
        c.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                day TEXT NOT NULL, event TEXT NOT NULL, value TEXT, source TEXT);
        """)
        c.execute("INSERT INTO events (ts, day, event, value, source) VALUES "
                  "(1, '1970-01-01', 'stove', 'off (ok)', 'voice')")
        c.execute("INSERT INTO events (ts, day, event, value, source) VALUES "
                  "(2, '1970-01-01', 'medicine', 'taken', 'conversation')")
        c.commit()
        c.close()
        d = Diary(old)
        kinds = d._conn.execute("SELECT event, kind FROM events ORDER BY id").fetchall()
        self.assertEqual(kinds, [("stove", "device"), ("medicine", "medication")])
        d._conn.close()
        Diary(old)._conn.close()      # opening twice is harmless

    def test_32_database_sits_next_to_diary_py(self):
        d = Diary("relative_name_test.db")
        try:
            self.assertEqual(Path(d.path).parent, Path(__file__).resolve().parent)
        finally:
            d._conn.close()
            Path(d.path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

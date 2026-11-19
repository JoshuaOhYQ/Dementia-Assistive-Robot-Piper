"""Tests for diary questions (steps 7-9). No broker, no ESP32, no API key needed.

    python test_diary_query.py

Gemini is replaced by fixed replies here, so these tests check OUR code: the
gatekeeper, the answers and the routing. How well the real Gemini picks the
query is measured separately by chat_eval.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.pop("GEMINI_API_KEY", None)          # always the offline stub here

import diary_query                              # noqa: E402
import gemini_client                            # noqa: E402
import pipeline                                 # noqa: E402
from diary import Diary                         # noqa: E402


class FakeState:
    def __init__(self, state):
        self.state, self.value = state, 0


class FakeHome:
    """Stands in for SmartHome: no MQTT, every command 'confirms'."""
    house_online = True

    def __init__(self):
        self.states = {"stove": FakeState("off"), "living_light": FakeState("on"),
                       "bedroom_light": FakeState("off"), "fan": FakeState("off")}
        self.sent = []

    def describe(self):
        return "stove off; living room light on."

    def command(self, device, action, value=None, source="voice"):
        self.sent.append((device, action))
        self.states[device].state = "off" if action == "off" else "on"
        return True, f"The {device} is now {self.states[device].state}."


def ts_at(day, hour, minute=0):
    return int(day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.diary = Diary(str(Path(self.tmp.name) / "q.db"))
        self.home = FakeHome()
        self.today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.yesterday = self.today - timedelta(days=1)
        self._real_ask = gemini_client.ask

    def tearDown(self):
        gemini_client.ask = self._real_ask
        self.diary._conn.close()
        self.tmp.cleanup()

    def fake_gemini(self, reply: dict):
        gemini_client.ask = lambda prompt, text: json.dumps(reply)

    def say(self, text):
        return pipeline.process(text, self.home, self.diary)


# ---------------------------------------------------------------------------
class TestGatekeeper(Base):
    def test_01_good_query(self):
        q, why = diary_query.validate({"kind": "medication", "window": "today"})
        self.assertEqual(q, {"kind": "medication", "window": "today", "thing": None})
        self.assertIsNone(why)

    def test_02_missing_window_means_today(self):
        q, _ = diary_query.validate({"kind": "alert"})
        self.assertEqual(q["window"], "today")

    def test_03_refuses_anything_off_the_menu(self):
        for bad in ({"kind": "medication", "window": "last_week"},
                    {"kind": "pills", "window": "today"},
                    {"kind": "device", "thing": "garage_door"},
                    {"kind": "medication", "window": "today'; DROP TABLE events;--"},
                    "medication today", None, []):
            q, why = diary_query.validate(bad)
            self.assertIsNone(q, bad)
            self.assertTrue(why)

    def test_04_thing_fills_in_kind(self):
        q, _ = diary_query.validate({"thing": "stove"})
        self.assertEqual((q["kind"], q["thing"]), ("device", "stove"))
        q, _ = diary_query.validate({"kind": "device", "thing": "medicine"})
        self.assertEqual((q["kind"], q["thing"]), ("medication", None))


# ---------------------------------------------------------------------------
class TestAnswers(Base):
    def test_10_no_medicine_is_honest_and_safe(self):
        s = diary_query.answer(self.diary, {"kind": "medication", "window": "today"})
        self.assertIn("don't have a note", s)
        self.assertIn("before taking any more", s)

    def test_11_medicine_times(self):
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.today, 8, 15))
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.today, 20, 5))
        s = diary_query.answer(self.diary, {"kind": "medication", "window": "today"})
        self.assertEqual(s, "Yes. I have a note that you took your medicine today "
                            "at 08:15 and 20:05.")

    def test_12_device_with_live_state(self):
        self.diary.log("stove", "cut off automatically after 20 min", "rule",
                       ts=ts_at(self.today, 12, 40))
        s = diary_query.answer(self.diary, {"kind": "device", "window": "today",
                                            "thing": "stove"}, home=self.home)
        self.assertIn("12:40", s)
        self.assertIn("Right now it is off.", s)

    def test_13_failed_command_described_honestly(self):
        self.diary.log("living_light", "on (failed)", "voice", ts=ts_at(self.today, 9))
        s = diary_query.answer(self.diary, {"kind": "device", "window": "today",
                                            "thing": "living_light"})
        self.assertIn("did not respond", s)

    def test_14_alerts(self):
        self.assertIn("Nothing worrying",
                      diary_query.answer(self.diary, {"kind": "alert", "window": "today"}))
        self.diary.log("stove", "warning, on 15 min unattended", "rule",
                       ts=ts_at(self.today, 11))
        s = diary_query.answer(self.diary, {"kind": "alert", "window": "today"})
        self.assertIn("11:00", s)

    def test_15_house_offline_gives_no_live_state(self):
        self.home.house_online = False
        s = diary_query.answer(self.diary, {"kind": "device", "window": "today",
                                            "thing": "stove"}, home=self.home)
        self.assertNotIn("Right now", s)


# ---------------------------------------------------------------------------
class TestRouting(Base):
    def test_20_the_yesterday_bug_is_fixed(self):
        # Taken YESTERDAY. The old code answered "Yes" to "today".
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.yesterday, 8))
        r = self.say("have I taken my medicine today?")
        self.assertNotIn("Yes", r["speech"])
        r = self.say("did I take my medicine yesterday?")
        self.assertTrue(r["speech"].startswith("Yes"))

    def test_21_tell_then_ask(self):
        self.say("I have taken my medicine")          # stub logs it
        r = self.say("did I take my pills this morning?" if datetime.now().hour < 12
                     else "did I take my pills today?")
        self.assertTrue(r["speech"].startswith("Yes"), r["speech"])

    def test_22_gemini_query_is_used_and_its_speech_ignored(self):
        self.fake_gemini({"speech": "Yes, you took them at 7am!", "commands": [],
                          "query": {"kind": "medication", "window": "today"}})
        r = self.say("did I take my tablets")
        self.assertIn("don't have a note", r["speech"])      # not the made-up 7am
        self.assertNotEqual(r["query"]["from"], "keywords")   # the model's query was used

    def test_23_gemini_answers_from_memory_safety_net_catches_it(self):
        self.fake_gemini({"speech": "Yes, you took your medicine.", "commands": []})
        r = self.say("have I taken my medicine today?")
        self.assertIn("don't have a note", r["speech"])
        self.assertEqual(r["query"]["from"], "keywords")

    def test_24_bad_window_refused(self):
        self.fake_gemini({"speech": "Let me check.", "commands": [],
                          "query": {"kind": "medication", "window": "last_week"}})
        r = self.say("did I take my medicine last week?")
        self.assertEqual(r["speech"], diary_query.NOT_ALLOWED_WINDOW)
        self.assertTrue(r["rejected"][0].startswith("QUERY"))

    def test_24b_unsupported_time_refused_even_offline(self):
        # Offline: "last week" must be refused, never answered as "today".
        self.diary.log("medicine", "taken", "conversation", ts=ts_at(self.today, 8))
        r = self.say("did I take my medicine last week?")
        self.assertEqual(r["speech"], diary_query.NOT_ALLOWED_WINDOW)
        self.assertEqual(diary_query.from_keywords("did I take my pills last night")["window"],
                         "yesterday")
        # "the light" alone is ambiguous (two lights), so no guess is made.
        self.assertIsNone(diary_query.from_keywords("did I turn the light off last night"))

    def test_24c_unsure_statement_is_checked_not_logged(self):
        # "I took it, didn't I?" must look it up, never write a new "taken" row.
        self.fake_gemini({"speech": "Good, I've noted that.", "commands": [],
                          "log": {"event": "medicine", "value": "taken"}})
        r = self.say("I took my medicine already, didn't I?")
        self.assertIn("don't have a note", r["speech"])
        self.assertEqual(self.diary.events_in("medication", "today"), [])

    def test_24d_stove_events_listed_in_order(self):
        self.diary.log("stove", "on (manual)", "manual", ts=ts_at(self.today, 9))
        self.diary.log("stove", "cut off automatically after 20 min", "rule",
                       ts=ts_at(self.today, 9, 35))
        s = diary_query.answer(self.diary, {"kind": "device", "window": "today",
                                            "thing": "stove"}, home=self.home)
        self.assertIn("turned on by hand at 09:00", s)
        self.assertIn("then was cut off automatically after 20 min at 09:35", s)

    def test_25_empty_query_on_chat_is_ignored(self):
        self.fake_gemini({"speech": "I'm well, thank you.", "commands": [],
                          "query": {"kind": "", "window": "", "thing": None}})
        r = self.say("how are you today")
        self.assertEqual(r["speech"], "I'm well, thank you.")
        self.assertIsNone(r["query"])

    def test_26_commands_win_over_query(self):
        self.fake_gemini({"speech": "Turning the fan off.",
                          "commands": [{"device": "fan", "action": "off"}],
                          "query": {"thing": "fan"}})
        r = self.say("turn off the fan, did I leave it on?")
        self.assertEqual(self.home.sent, [("fan", "off")])
        self.assertIsNone(r["query"])

    def test_26b_no_voice_control_of_the_stove(self):
        # Even if the model tries, "turn off the stove" sends nothing.
        self.fake_gemini({"speech": "Turning the stove off.",
                          "commands": [{"device": "stove", "action": "off"}]})
        r = self.say("turn off the stove")
        self.assertEqual(self.home.sent, [])
        self.assertIn("already off", r["speech"])            # FakeHome stove is off
        self.home.states["stove"].state = "on"
        r = self.say("turn off the stove")
        self.assertEqual(self.home.sent, [])
        self.assertIn("knob", r["speech"])
        r = self.say("please switch the cooker off")         # offline stub path
        self.assertEqual(self.home.sent, [])

    def test_27_stove_on_refused(self):
        r = self.say("turn on the stove")
        self.assertEqual(self.home.sent, [])
        self.assertIsNone(r["query"])

    def test_28_commands_still_work_and_are_logged(self):
        self.say("turn on the bedroom light")
        self.assertEqual(self.home.sent, [("bedroom_light", "on")])
        r = self.say("did I turn on the bedroom light?")
        self.assertIn("was turned on", r["speech"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

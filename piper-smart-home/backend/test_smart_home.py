"""End-to-end tests for the PIPER smart-home MQTT layer.

Starts the virtual house node in-process, then drives the backend through it.
Requires a broker on localhost:1883.

    python test_smart_home.py

This doubles as the evidence for the report's testing section: it exercises the
command path, the state feedback path, the safety interlock, the LLM validator
and the two most important rules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Scratch database for the tests. tempfile.gettempdir() resolves to /tmp on
# Linux/macOS and %LOCALAPPDATA%\Temp on Windows — a hardcoded "/tmp/..." fails
# on Windows with sqlite3.OperationalError: unable to open database file.
TEST_DB = str(Path(tempfile.gettempdir()) / "piper_test.db")

os.environ.setdefault("PIPER_TOPIC_ROOT", "pipertest")
os.environ.setdefault("PIPER_DB", TEST_DB)

import command_bridge          # noqa: E402
import config                  # noqa: E402
import gemini_client           # noqa: E402
import pipeline                # noqa: E402
from diary import Diary        # noqa: E402
from rules import RuleEngine   # noqa: E402
from smart_home import SmartHome  # noqa: E402

SIM = Path(__file__).resolve().parents[1] / "sim" / "virtual_house.py"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.house = subprocess.Popen(
            [sys.executable, "-u", str(SIM)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=os.environ.copy(),
        )
        time.sleep(2.0)
        cls.diary = Diary(TEST_DB)
        cls.home = SmartHome(on_state_change=cls.diary.state_logger())
        assert cls.home.connect(timeout=5), "broker not reachable on localhost:1883"
        time.sleep(1.5)

    @classmethod
    def tearDownClass(cls):
        cls.home.stop()
        cls.house.terminate()
        try:
            cls.house.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.house.kill()


class TestConnectivity(Base):
    def test_01_house_is_online(self):
        self.assertTrue(self.home.house_online, "LWT/availability did not report online")

    def test_02_retained_state_received(self):
        for dev in config.DEVICES:
            self.assertIn(self.home.states[dev].state, ("on", "off"),
                          f"no retained state for {dev}")


class TestCommands(Base):
    def test_10_switch_on_and_confirm(self):
        ok, msg = self.home.command("living_light", "on", source="voice")
        self.assertTrue(ok, msg)
        self.assertEqual(self.home.states["living_light"].state, "on")

    def test_11_switch_off(self):
        ok, _ = self.home.command("living_light", "off", source="voice")
        self.assertTrue(ok)
        self.assertEqual(self.home.states["living_light"].state, "off")

    def test_12_dimmable_set(self):
        ok, msg = self.home.command("fan", "set", 60, source="voice")
        self.assertTrue(ok, msg)
        self.assertEqual(self.home.states["fan"].value, 60)
        self.assertEqual(self.home.states["fan"].state, "on")
        self.assertIn("60", msg)

    def test_13_unknown_device_rejected_locally(self):
        ok, msg = self.home.command("microwave", "on")
        self.assertFalse(ok)
        self.assertIn("don't know", msg)

    def test_14_stove_on_refused_by_backend(self):
        ok, msg = self.home.command("stove", "on", source="voice")
        self.assertFalse(ok)
        self.assertIn("knob", msg.lower())
        self.assertEqual(self.home.states["stove"].state, "off")

    def test_14b_stove_off_by_voice_refused_by_backend(self):
        """No voice control of the stove at all, not even 'off'."""
        ok, msg = self.home.command("stove", "off", source="voice")
        self.assertFalse(ok)
        self.assertIn("knob", msg.lower())

    def test_15_stove_off_allowed(self):
        ok, _ = self.home.command("stove", "off", source="rule")
        self.assertTrue(ok)


class TestLLMValidation(Base):
    def test_20_valid_command_accepted(self):
        acc, rej = command_bridge.validate_commands(
            [{"device": "fan", "action": "set", "value": 250}])
        self.assertEqual(acc, [{"device": "fan", "action": "set", "value": 100}])
        self.assertFalse(rej)

    def test_21_hallucinated_device_dropped(self):
        acc, rej = command_bridge.validate_commands(
            [{"device": "garage_door", "action": "on"}])
        self.assertEqual(acc, [])
        self.assertTrue(any("unknown device" in r for r in rej))

    def test_22_stove_on_blocked_at_validator(self):
        acc, rej = command_bridge.validate_commands([{"device": "stove", "action": "on"}])
        self.assertEqual(acc, [])
        self.assertTrue(any(r.startswith("SAFETY") for r in rej))

    def test_22b_stove_off_blocked_at_validator(self):
        acc, rej = command_bridge.validate_commands([{"device": "stove", "action": "off"}])
        self.assertEqual(acc, [])
        self.assertTrue(any("not voice-controlled" in r for r in rej))

    def test_22c_stove_not_offered_to_the_model(self):
        prompt = command_bridge.build_system_prompt(self.home)
        devices = prompt.split("DEVICES YOU MAY CONTROL:")[1].split("You MUST")[0]
        self.assertNotIn('"stove"', devices)

    def test_23_command_flood_capped(self):
        acc, rej = command_bridge.validate_commands(
            [{"device": "living_light", "action": "toggle"}] * 10)
        self.assertEqual(len(acc), config.MAX_COMMANDS_PER_UTTERANCE)
        self.assertTrue(any("dropped" in r for r in rej))

    def test_24_parses_fenced_json(self):
        raw = '```json\n{"speech":"Okay.","commands":[{"device":"fan","action":"on"}]}\n```'
        d = command_bridge.parse_llm_reply(raw)
        self.assertEqual(d["speech"], "Okay.")
        self.assertEqual(len(d["commands"]), 1)

    def test_26_model_cannot_claim_what_did_not_happen(self):
        """If the model says 'turning the stove on', the robot must not repeat it."""
        lie = {"speech": "Sure, turning the stove on now!",
               "commands": [{"device": "stove", "action": "on"}]}
        res = command_bridge.execute_detailed(self.home, lie)
        self.assertEqual(res["accepted"], [])
        self.assertNotIn("turning the stove on", res["speech"].lower())
        self.assertEqual(res["speech"], command_bridge.SAFETY_LINE)

    def test_25_survives_garbage(self):
        d = command_bridge.parse_llm_reply("I'm sorry, I didn't catch that")
        self.assertEqual(d["commands"], [])
        self.assertTrue(d["speech"])


class TestRules(Base):
    def setUp(self):
        self.engine = RuleEngine(self.home, self.diary,
                                 speak=lambda t: self.spoken.append(t),
                                 alert=lambda t: self.alerts.append(t))
        self.spoken, self.alerts = [], []
        self.engine.speak = lambda t: self.spoken.append(t)
        self.engine.alert = lambda t: self.alerts.append(t)

    def _wait_state(self, dev, want, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.home.states[dev].state == want:
                return True
            time.sleep(0.1)
        return False

    def test_30_night_path_lighting(self):
        self.home.command("living_light", "off")
        self.home.sensors["living_occupancy"] = {"value": True}
        night = datetime.now().replace(hour=23, minute=0)
        self.engine.tick(now=night)
        self.assertTrue(self._wait_state("living_light", "on"),
                        "night path lighting did not switch the light on")
        self.assertEqual(self.home.states["living_light"].source, "rule")

    def test_30b_night_rule_does_not_fight_the_user(self):
        """Regression: at night, with someone in the room, the person switches the
        light off by voice. The rule must NOT switch it back on."""
        night = datetime.now().replace(hour=23, minute=0)
        self.home.sensors["living_occupancy"] = {"value": True}
        self.engine.tick(now=night)                              # arrival -> light on
        self.assertTrue(self._wait_state("living_light", "on"))

        ok, _ = self.home.command("living_light", "off", source="voice")
        self.assertTrue(ok)
        for _ in range(3):                                       # 3 more ticks, still occupied
            self.engine.tick(now=night)
            time.sleep(0.2)
        self.assertEqual(self.home.states["living_light"].state, "off",
                         "night rule switched the light back on against the user's wish")

    def test_31_empty_room_light_off(self):
        self.home.command("living_light", "on")
        self.home.sensors["living_occupancy"] = {"value": False}
        self.engine._night_light_on_by_rule = False
        self.engine._empty_since = time.time() - (config.RULES["empty_room_light_off_s"] + 10)
        day = datetime.now().replace(hour=14, minute=0)
        self.engine.tick(now=day)
        self.assertTrue(self._wait_state("living_light", "off"),
                        "empty-room rule did not switch the light off")

    def test_32_unattended_stove_warns_then_cuts_off(self):
        # Human switches the stove on by hand (the AI cannot).
        self.house.stdin.write("s\n")
        self.house.stdin.flush()
        self.assertTrue(self._wait_state("stove", "on"), "manual stove-on did not register")

        self.home.sensors["living_occupancy"] = {"value": False}
        self.engine._empty_since = time.time() - 1000

        # 16 minutes unattended -> spoken warning, stove still on
        self.home.sensors["stove_on_seconds"] = {"value": 960}
        self.engine.tick()
        self.assertTrue(self.spoken, "no warning was spoken at 16 min")
        self.assertEqual(self.home.states["stove"].state, "on")

        # 21 minutes -> automatic cut-off plus a caregiver alert
        self.home.sensors["stove_on_seconds"] = {"value": 1260}
        self.engine.tick()
        self.assertTrue(self._wait_state("stove", "off"), "stove was not cut off")
        self.assertTrue(self.alerts, "no Telegram alert was raised")


class TestDiary(Base):
    def test_40_write_and_read_back(self):
        self.diary.log("medicine", "taken", source="conversation")
        rows = self.diary.search("medicine")
        self.assertTrue(rows)
        self.assertEqual(rows[0][1], "medicine")

    def test_41_formats_for_prompt(self):
        lines = Diary.as_lines(self.diary.today(limit=5))
        self.assertTrue(all("—" in l for l in lines))


class TestStoveKnobLink(Base):
    """Step 6: the stove knob (GPIO 32 on the board, key 's' in the virtual
    house) reaches the diary over MQTT, so PIPER can answer about it later."""

    def _knob(self):
        self.house.stdin.write("s\n")
        self.house.stdin.flush()

    def _wait(self, check, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def _stove_rows(self):
        return self.diary._conn.execute(
            "SELECT value, source, kind FROM events WHERE event='stove' "
            "ORDER BY id").fetchall()

    def test_50_knob_on_is_logged_as_manual(self):
        before = len(self._stove_rows())
        self._knob()
        self.assertTrue(self._wait(lambda: self.home.states["stove"].state == "on"))
        self.assertTrue(self._wait(lambda: len(self._stove_rows()) > before),
                        "the knob did not reach the diary")
        self.assertEqual(self._stove_rows()[-1], ("on (manual)", "manual", "device"))

    def test_51_pipeline_answers_from_the_knob_event(self):
        saved, gemini_client._model = gemini_client._model, None   # offline stub
        try:
            r = pipeline.process("did I turn on the stove?", self.home, self.diary)
        finally:
            gemini_client._model = saved
        self.assertIn("by hand", r["speech"])
        self.assertIn("Right now it is on", r["speech"])

    def test_52_house_itself_refuses_a_voice_stove_command(self):
        """Third layer: even a raw MQTT command that skips the backend checks
        is refused by the house node when it comes from 'voice'."""
        self.assertTrue(self._wait(lambda: self.home.states["stove"].state == "on"))
        before = len(self._stove_rows())
        self.home.client.publish(f"{config.TOPIC_ROOT}/cmd/stove", json.dumps(
            {"action": "off", "req_id": "vtest1", "source": "voice", "ts": 0}), qos=1)
        time.sleep(1.0)
        self.assertEqual(self.home.states["stove"].state, "on")
        self.assertEqual(len(self._stove_rows()), before)      # nothing changed, nothing logged
        self._knob()                                            # person switches it off
        self.assertTrue(self._wait(lambda: self.home.states["stove"].state == "off"))

    def test_53_knob_off_is_logged(self):
        self._knob()                                   # off -> on
        self.assertTrue(self._wait(lambda: self.home.states["stove"].state == "on"))
        self._knob()                                   # on -> off
        self.assertTrue(self._wait(lambda: self.home.states["stove"].state == "off"))
        self.assertTrue(self._wait(lambda: self._stove_rows()[-1][0] == "off (manual)"))


if __name__ == "__main__":
    unittest.main(verbosity=2, failfast=False)

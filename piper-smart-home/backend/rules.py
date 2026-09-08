"""PIPER backend — the home-monitoring branch.

These are the automatic behaviours from section 3.2 of the report: the things
PIPER does when nobody is talking to it and nobody has fallen. They live in the
backend rather than the ESP32 because they need the clock and the diary.

The engine is deliberately a plain polled state machine, one pass per tick, to
match the report's single-loop design. Each rule is independent and each one
logs what it did and why.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import config
from diary import Diary
from smart_home import SmartHome

log = logging.getLogger("piper.rules")


class RuleEngine:
    def __init__(self, home: SmartHome, diary: Diary, speak=None, alert=None):
        self.home = home
        self.diary = diary
        self.speak = speak or (lambda text: log.info("SPEAK: %s", text))
        self.alert = alert or (lambda text: log.warning("TELEGRAM: %s", text))

        self._empty_since: float | None = None      # living room empty since
        self._night_light_on_by_rule = False
        self._stove_warned = False

    # -- helpers -----------------------------------------------------------

    def _is_night(self, now: datetime | None = None) -> bool:
        h = (now or datetime.now()).hour
        s, e = config.RULES["night_start_hour"], config.RULES["night_end_hour"]
        return h >= s or h < e          # spans midnight

    def _occupied(self) -> bool | None:
        v = self.home.sensors.get("living_occupancy", {}).get("value")
        return None if v is None else bool(v)

    def _stove_seconds(self) -> int:
        return int(self.home.sensors.get("stove_on_seconds", {}).get("value", 0))

    # -- the tick ----------------------------------------------------------

    def tick(self, now: datetime | None = None):
        """Call this once every few seconds from the idle branch of the main loop."""
        if not self.home.house_online:
            return

        occ = self._occupied()
        if occ is None:
            return                       # no occupancy data yet, do nothing

        # keep the "how long has the room been empty" timer
        if occ:
            self._empty_since = None
        elif self._empty_since is None:
            self._empty_since = time.time()

        self._rule_night_path_light(occ, now)
        self._rule_unattended_stove(occ)
        self._rule_empty_room_light(occ)

    # -- rule 1: light the way at night ------------------------------------

    def _rule_night_path_light(self, occupied: bool, now=None):
        light = self.home.states["living_light"]
        if occupied and self._is_night(now) and light.state == "off":
            ok, _ = self.home.command("living_light", "on", source="rule")
            if ok:
                self._night_light_on_by_rule = True
                self.diary.log("living_light", "on (night path lighting)", source="rule")
                log.info("RULE night path lighting: living_light ON")

        elif (self._night_light_on_by_rule and not occupied
              and self._empty_since
              and time.time() - self._empty_since > config.RULES["night_light_auto_off_s"]):
            ok, _ = self.home.command("living_light", "off", source="rule")
            if ok:
                self._night_light_on_by_rule = False
                self.diary.log("living_light", "off (auto, room empty)", source="rule")

    # -- rule 2: the stove ---------------------------------------------------

    def _rule_unattended_stove(self, occupied: bool):
        stove = self.home.states["stove"]
        if stove.state != "on":
            self._stove_warned = False
            return

        on_for = self._stove_seconds()

        if occupied:
            # Someone is cooking. Reset the warning so it fires from scratch if
            # they walk away later.
            self._stove_warned = False
            return

        if on_for >= config.RULES["stove_cutoff_after_s"]:
            ok, _ = self.home.command("stove", "off", source="rule")
            self.diary.log("stove", f"cut off automatically after {on_for//60} min", source="rule")
            self.alert(f"PIPER: the stove was left on for {on_for//60} minutes with "
                       f"nobody in the room. It has been switched off automatically.")
            self.speak("I have turned the stove off for you, it was left on for a while.")
            log.warning("RULE stove cutoff after %ss (published=%s)", on_for, ok)

        elif on_for >= config.RULES["stove_warn_after_s"] and not self._stove_warned:
            self._stove_warned = True
            self.diary.log("stove", f"warning, on {on_for//60} min unattended", source="rule")
            self.speak("The stove has been on for a while and nobody is in the kitchen. "
                       "Would you like me to turn it off?")
            log.info("RULE stove warning at %ss", on_for)

    # -- rule 3: light left on in an empty room ------------------------------

    def _rule_empty_room_light(self, occupied: bool):
        if occupied or self._empty_since is None:
            return
        if self._night_light_on_by_rule:
            return                       # rule 1 owns the light right now
        empty_for = time.time() - self._empty_since
        light = self.home.states["living_light"]
        if light.state == "on" and empty_for > config.RULES["empty_room_light_off_s"]:
            ok, _ = self.home.command("living_light", "off", source="rule")
            if ok:
                self.diary.log("living_light", f"off (empty {int(empty_for//60)} min)",
                               source="rule")
                log.info("RULE empty-room light off after %.0fs", empty_for)

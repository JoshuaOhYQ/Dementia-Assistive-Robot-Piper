"""PIPER backend — one pass of the conversation branch, with timings.

Text in (from Whisper, the keyboard, or the robot ESP32 over HTTP), spoken line
out. Used by both main.py (keyboard) and server.py (robot ESP32), so the two
entry points can never drift apart.
"""
from __future__ import annotations

import time

import command_bridge
import gemini_client
from diary import Diary
from smart_home import SmartHome

_MEMORY_KEYWORDS = ("medicine", "pill", "stove", "light", "fan")


def llm_mode() -> str:
    return "gemini" if gemini_client._model is not None else "offline-stub"


def process(text: str, home: SmartHome, diary: Diary, source: str = "voice") -> dict:
    """Run one utterance through prompt → LLM → validator → MQTT.

    Returns speech plus everything needed for the test log: what was accepted,
    what was refused, whether the hardware confirmed, and where the time went.
    """
    t_start = time.perf_counter()

    diary_lines = Diary.as_lines(diary.today(limit=12))
    prompt = command_bridge.build_system_prompt(home, diary_lines)

    t_llm = time.perf_counter()
    raw = gemini_client.ask(prompt, text)
    llm_ms = round((time.perf_counter() - t_llm) * 1000, 1)

    reply = command_bridge.parse_llm_reply(raw)

    # Memory questions ("have I taken my pills?") are answered from the diary
    # directly, so this works even with no LLM at all.
    lowered = text.lower()
    if not reply.get("commands") and any(w in lowered for w in ("have i", "did i")):
        for kw in _MEMORY_KEYWORDS:
            if kw in lowered:
                row = diary.last(kw)
                if row:
                    ts, event, value, _ = row
                    speech = (f"Yes — at {time.strftime('%H:%M', time.localtime(ts))} "
                              f"I recorded {event}: {value}.")
                else:
                    speech = "I don't have a record of that today."
                return _result(speech, [], [], [], True, 0.0, llm_ms, t_start)

    res = command_bridge.execute_detailed(home, reply, diary=diary, source=source)
    return _result(res["speech"], res["accepted"], res["rejected"], res["results"],
                   res["all_ok"], res["mqtt_ms"], llm_ms, t_start)


def _result(speech, accepted, rejected, results, all_ok, mqtt_ms, llm_ms, t_start) -> dict:
    return {
        "speech": speech,
        "accepted": accepted,
        "rejected": rejected,
        "results": results,
        "all_ok": all_ok,
        "llm_mode": llm_mode(),
        "timing_ms": {
            "llm": llm_ms,
            "mqtt_roundtrip": mqtt_ms,
            "backend_total": round((time.perf_counter() - t_start) * 1000, 1),
        },
    }

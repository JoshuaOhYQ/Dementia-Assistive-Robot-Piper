"""PIPER backend — one pass of the conversation branch, with timings.

Text in (from Whisper, the keyboard, or the robot ESP32 over HTTP), spoken line
out. Used by both main.py (keyboard) and server.py (robot ESP32), so the two
entry points can never drift apart.

Three routes, checked in this order:

  1. commands  -> validated and sent over MQTT (command_bridge)
  2. query     -> questions about the past, answered from the diary (diary_query)
  3. chat      -> the model's own sentence
"""
from __future__ import annotations

import time

import command_bridge
import diary_query
import gemini_client
from diary import Diary
from smart_home import SmartHome


def llm_mode() -> str:
    return "gemini" if gemini_client._model is not None else "offline-stub"


def process(text: str, home: SmartHome, diary: Diary, source: str = "voice") -> dict:
    """Run one utterance through prompt → LLM → validator → MQTT or diary.

    Returns speech plus everything needed for the test log: what was accepted,
    what was refused, whether the hardware confirmed, which diary lookup ran,
    and where the time went.
    """
    t_start = time.perf_counter()

    diary_lines = Diary.as_lines(diary.today(limit=12))
    prompt = command_bridge.build_system_prompt(home, diary_lines)

    t_llm = time.perf_counter()
    raw = gemini_client.ask(prompt, text)
    llm_ms = round((time.perf_counter() - t_llm) * 1000, 1)

    reply = command_bridge.parse_llm_reply(raw)

    # Route 2: a question about the past. Commands always win, because an
    # action the person asked for matters more than a lookup.
    if not reply.get("commands"):
        raw_query = reply.get("query")
        query_from = "llm" if gemini_client._model is not None else "stub"
        if not _has_query(raw_query):
            # Safety net: offline, or the model answered from memory instead
            # of asking the diary. Never let that answer stand.
            raw_query = diary_query.from_keywords(text)
            query_from = "keywords"
        if raw_query:
            return _answer_query(raw_query, query_from, home, diary, llm_ms, t_start)

    # Routes 1 and 3: commands and chat.
    res = command_bridge.execute_detailed(home, reply, diary=diary, source=source)
    return _result(res["speech"], res["accepted"], res["rejected"], res["results"],
                   res["all_ok"], res["mqtt_ms"], llm_ms, t_start)


def _has_query(q) -> bool:
    """Models sometimes send an empty query on every reply. Only count a real one."""
    return isinstance(q, dict) and bool(q.get("kind") or q.get("thing"))


def _answer_query(raw_query, query_from, home, diary, llm_ms, t_start) -> dict:
    t_db = time.perf_counter()
    query, why = diary_query.validate(raw_query)
    if query is None:
        speech, rejected = diary_query.refusal_line(why), [f"QUERY: {why}"]
    else:
        speech, rejected = diary_query.answer(diary, query, home=home), []
    db_ms = round((time.perf_counter() - t_db) * 1000, 1)

    out = _result(speech, [], rejected, [], True, 0.0, llm_ms, t_start)
    out["query"] = {**(query or {"raw": raw_query}), "from": query_from}
    out["timing_ms"]["diary"] = db_ms
    return out


def _result(speech, accepted, rejected, results, all_ok, mqtt_ms, llm_ms, t_start) -> dict:
    return {
        "speech": speech,
        "accepted": accepted,
        "rejected": rejected,
        "results": results,
        "all_ok": all_ok,
        "llm_mode": llm_mode(),
        "query": None,
        "timing_ms": {
            "llm": llm_ms,
            "mqtt_roundtrip": mqtt_ms,
            "backend_total": round((time.perf_counter() - t_start) * 1000, 1),
        },
    }

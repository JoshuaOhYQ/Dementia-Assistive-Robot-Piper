"""PIPER backend — the bridge between Gemini's output and MQTT.

Two jobs:

1. Build the prompt fragment that tells Gemini what it may control.
2. Take Gemini's JSON back, validate it hard, and execute it.

The validation here is the whole point. An LLM is a probabilistic component in
a safety-adjacent system; it is treated as an untrusted input source. Nothing
it says reaches an appliance without passing every check below.
"""
from __future__ import annotations

import json
import logging
import re
import time

import config
from diary import KINDS, WINDOWS
from smart_home import SmartHome

log = logging.getLogger("piper.bridge")

# ---------------------------------------------------------------------------
# 1. Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt(home: SmartHome, diary_lines: list[str] | None = None) -> str:
    device_lines = []
    for dev_id, d in config.DEVICES.items():
        if not d.get("voice_control", True):
            continue                       # e.g. the stove: never offered to the model
        acts = "/".join(d["actions"])
        note = "" if d["allow_on"] else "  (you may ONLY turn this off, never on)"
        device_lines.append(f'  - "{dev_id}"  = the {d["friendly"]}, actions: {acts}{note}')

    diary = "\n".join(f"  - {l}" for l in (diary_lines or [])) or "  - (nothing recorded yet)"
    kinds = ", ".join(f'"{k}"' for k in KINDS)
    windows = ", ".join(f'"{w}"' for w in WINDOWS)
    things = ", ".join(f'"{t}"' for t in list(config.DEVICES) + ["medicine"])

    return f"""You are PIPER, a calm and patient companion robot for an elderly person
living with early-to-moderate dementia. Speak in short, plain sentences. Never rush
them, never correct them harshly, and never mention that you are an AI model.

CURRENT STATE OF THE HOME:
  {home.describe()}

WHAT THEY HAVE DONE TODAY (from your memory):
{diary}

DEVICES YOU MAY CONTROL:
{chr(10).join(device_lines)}

You MUST reply with a single JSON object and nothing else:

{{
  "speech": "<what you say out loud, one or two short sentences>",
  "commands": [ {{"device": "<device_id>", "action": "<action>", "value": <0-100, only for set>}} ],
  "log": {{"event": "<short label>", "value": "<short value>"}},
  "query": {{"kind": "<kind>", "window": "<window>", "thing": "<thing or null>"}}
}}

Rules:
- "commands" is [] when the person is only chatting or asking a question.
- Never put more than {config.MAX_COMMANDS_PER_UTTERANCE} commands in one reply.
- You can NEVER control the stove, not on and not off; it is not in the list
  above. If they ask you to switch it on or off, put no command, and kindly say
  they need to use the knob on the stove themselves. You may tell them whether it
  is on right now (CURRENT STATE OF THE HOME) or answer about it with "query".
- If you are not sure which device they mean, ask them in "speech" and return no commands.
- "log" is optional; include it when something worth remembering happened
  (medicine taken, a meal, a visitor, feeling unwell). When they say they have
  taken their medicine, use exactly {{"event": "medicine", "value": "taken"}}.
- "query" is optional; include it ONLY when they ask about the PAST, such as
  "did I take my pills?", "have I turned the stove off?", "what did I do this
  morning?". Then "commands" must be [] and "speech" can be just "Let me check."
  because the system answers from its records, not from you.
    kind   = one of: {kinds}
             medication = medicine or pills, device = lights/fan/stove,
             alert = safety warnings, note = other things worth remembering
    window = one of: {windows}   (use "today" if they don't say)
    thing  = one of: {things}, or null if they don't name one
  If they ask about a time not in that list (for example "last week"), still
  send the query with the window they used; the system will explain.
- Never say from your own memory whether they took their medicine or switched
  something off. Always use "query" for that.
- Questions about NOW ("is the light on?") are answered from CURRENT STATE OF
  THE HOME above, without a query.
"""


# ---------------------------------------------------------------------------
# 2. Parsing and validation
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_llm_reply(raw: str) -> dict:
    """Gemini sometimes wraps JSON in ```json fences or adds a sentence. Be tolerant."""
    if not raw:
        return {"speech": "Sorry, could you say that again?", "commands": []}
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = _JSON_BLOCK.search(text)
    if not m:
        # The model spoke plain prose — treat the whole thing as speech.
        return {"speech": text[:300], "commands": []}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {"speech": "Sorry, could you say that again?", "commands": []}
    if not isinstance(data, dict):
        return {"speech": "Sorry, could you say that again?", "commands": []}
    data.setdefault("speech", "")
    data.setdefault("commands", [])
    if not isinstance(data["commands"], list):
        data["commands"] = []
    return data


def validate_commands(commands: list) -> tuple[list[dict], list[str]]:
    """Return (accepted, rejection_reasons). Nothing invalid ever gets through."""
    accepted, rejected = [], []

    for c in commands[: config.MAX_COMMANDS_PER_UTTERANCE]:
        if not isinstance(c, dict):
            rejected.append("command was not an object")
            continue

        dev_id = str(c.get("device", "")).strip()
        action = str(c.get("action", "")).strip().lower()
        dev = config.DEVICES.get(dev_id)

        if dev is None:
            rejected.append(f"unknown device '{dev_id}'")
            continue
        # Devices with no voice control (the stove) are refused for EVERY action,
        # including "off". Only the knob and the automatic safety rule touch it.
        if not dev.get("voice_control", True):
            rejected.append(f"SAFETY: '{dev_id}' is not voice-controlled (action={action})")
            continue
        # The safety interlock is checked FIRST, so that an attempt to energise a
        # protected load is always reported as a safety refusal and never gets
        # buried in a generic "unknown action" message. The distinction matters:
        # one is a model mistake, the other is a model overreach, and the robot
        # says something different in each case.
        if action in ("on", "toggle", "set") and not dev["allow_on"]:
            rejected.append(f"SAFETY: refused to switch '{dev_id}' on")
            continue
        if action not in dev["actions"]:
            rejected.append(f"action '{action}' not allowed for '{dev_id}'")
            continue

        out = {"device": dev_id, "action": action}
        if action == "set":
            if dev["type"] != config.DIMMABLE:
                rejected.append(f"'{dev_id}' is not dimmable")
                continue
            try:
                out["value"] = max(0, min(100, int(c.get("value", 100))))
            except (TypeError, ValueError):
                rejected.append(f"bad value for '{dev_id}'")
                continue
        accepted.append(out)

    if len(commands) > config.MAX_COMMANDS_PER_UTTERANCE:
        rejected.append(f"dropped {len(commands) - config.MAX_COMMANDS_PER_UTTERANCE} extra commands")

    for r in rejected:
        log.warning("rejected command: %s", r)
    return accepted, rejected


# ---------------------------------------------------------------------------
# 3. Execution
# ---------------------------------------------------------------------------

SAFETY_LINE = ("For your safety I can't switch that on. "
                "If you'd like to use it, please turn it on yourself and I'll remind you later.")
UNKNOWN_LINE = "Sorry, I'm not able to control that."
STOVE_KNOB_LINE = ("I'm not able to control the stove. "
                   "Please use the knob on the stove yourself.")


def _safety_speech(home, rejections: list[str]) -> str:
    """What to say when every command was refused for safety."""
    stove_off = any("not voice-controlled" in r and "action=off" in r for r in rejections)
    if not stove_off:
        return SAFETY_LINE                       # an attempt to switch something ON
    st = getattr(home, "states", {}).get("stove")
    if getattr(home, "house_online", False) and st is not None and st.state == "off":
        return "The stove is already off, so there's nothing to do."
    return STOVE_KNOB_LINE


def execute_detailed(home: SmartHome, llm_reply: dict, diary=None,
                     source: str = "voice") -> dict:
    """Run the validated commands and report exactly what happened.

    Returns a dict with the final spoken line plus per-command results and
    timings — what the HTTP server returns to the robot ESP32 and writes to the
    test log for the report.
    """
    speech = str(llm_reply.get("speech", "")).strip()
    commands, rejections = validate_commands(llm_reply.get("commands", []))

    results = []
    for c in commands:
        t0 = time.perf_counter()
        ok, msg = home.command(c["device"], c["action"], c.get("value"), source=source)
        mqtt_ms = round((time.perf_counter() - t0) * 1000, 1)
        results.append({**c, "ok": ok, "message": msg, "mqtt_ms": mqtt_ms})
        if diary is not None:
            diary.log(event=c["device"], value=f'{c["action"]} ({"ok" if ok else "failed"})',
                      source=source)

    safety_hit = any(r.startswith("SAFETY") for r in rejections)
    failures = [r["message"] for r in results if not r["ok"]]

    # Never let the model's own sentence stand when the system did something
    # different from what that sentence claims.
    if failures:
        speech = " ".join(failures)             # hardware did not confirm
    elif rejections and not commands:
        speech = _safety_speech(home, rejections) if safety_hit else UNKNOWN_LINE
    elif safety_hit:
        speech = (speech + " " if speech else "") + "I can't control the stove, though."

    if not failures and diary is not None and isinstance(llm_reply.get("log"), dict):
        entry = llm_reply["log"]
        if entry.get("event"):
            diary.log(event=str(entry.get("event", ""))[:60],
                      value=str(entry.get("value", ""))[:60], source="conversation")

    return {
        "speech": speech or "Okay.",
        "accepted": commands,
        "rejected": rejections,
        "results": results,
        "all_ok": not failures,
        "mqtt_ms": round(sum(r["mqtt_ms"] for r in results), 1),
    }


def execute(home: SmartHome, llm_reply: dict, diary=None) -> str:
    """Run the validated commands and return the final line for text-to-speech."""
    return execute_detailed(home, llm_reply, diary)["speech"]

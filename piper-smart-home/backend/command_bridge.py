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

import config
from smart_home import SmartHome

log = logging.getLogger("piper.bridge")

# ---------------------------------------------------------------------------
# 1. Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt(home: SmartHome, diary_lines: list[str] | None = None) -> str:
    device_lines = []
    for dev_id, d in config.DEVICES.items():
        acts = "/".join(d["actions"])
        note = "" if d["allow_on"] else "  (you may ONLY turn this off, never on)"
        device_lines.append(f'  - "{dev_id}"  = the {d["friendly"]}, actions: {acts}{note}')

    diary = "\n".join(f"  - {l}" for l in (diary_lines or [])) or "  - (nothing recorded yet)"

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
  "log": {{"event": "<short label>", "value": "<short value>"}}
}}

Rules:
- "commands" is [] when the person is only chatting or asking a question.
- Never put more than {config.MAX_COMMANDS_PER_UTTERANCE} commands in one reply.
- Never switch the stove ON. If they ask you to, gently say you cannot and suggest
  they do it themselves, and offer to remind them to turn it off later.
- If you are not sure which device they mean, ask them in "speech" and return no commands.
- "log" is optional; include it when something worth remembering happened
  (medicine taken, a meal, a visitor, feeling unwell).
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

def execute(home: SmartHome, llm_reply: dict, diary=None) -> str:
    """Run the validated commands and return the final line for text-to-speech."""
    speech = str(llm_reply.get("speech", "")).strip()
    commands, rejections = validate_commands(llm_reply.get("commands", []))

    failures = []
    for c in commands:
        ok, msg = home.command(c["device"], c["action"], c.get("value"), source="voice")
        if diary is not None:
            diary.log(event=c["device"], value=f'{c["action"]} ({"ok" if ok else "failed"})',
                      source="voice")
        if not ok:
            failures.append(msg)

    if rejections and not commands:
        # The model asked for something it is not allowed to do at all.
        if any(r.startswith("SAFETY") for r in rejections):
            return (speech or "") + " I'm not able to switch that on for you, " \
                                    "but I can remind you about it later."

    if failures:
        # Do not let the robot claim success when the hardware said nothing.
        return " ".join(failures)

    if diary is not None and isinstance(llm_reply.get("log"), dict):
        entry = llm_reply["log"]
        diary.log(event=str(entry.get("event", ""))[:60],
                  value=str(entry.get("value", ""))[:60], source="conversation")

    return speech or "Okay."

"""PIPER backend — Gemini wrapper with an offline fallback.

If GEMINI_API_KEY is set and google-generativeai is installed, real Gemini is
used. Otherwise a small rule-based stub stands in, so the whole MQTT pipeline
can be developed, demoed and marked without an API key or an internet
connection. The stub returns exactly the same JSON shape as the real model,
which is the point of having a strict contract in the first place.
"""
from __future__ import annotations

import json
import logging
import os
import re

import config

log = logging.getLogger("piper.gemini")

API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

_model = None
if API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=API_KEY)
        _model = genai.GenerativeModel(MODEL_NAME)
        log.info("using Gemini model %s", MODEL_NAME)
    except Exception as e:                      # noqa: BLE001
        log.warning("Gemini unavailable (%s) — falling back to the offline stub", e)


def ask(system_prompt: str, user_text: str) -> str:
    """Return the model's raw text. Falls back to the stub on any failure."""
    if _model is not None:
        try:
            resp = _model.generate_content(
                [system_prompt, f"The person said: {user_text}"],
                generation_config={"response_mime_type": "application/json",
                                   "temperature": 0.4},
            )
            return resp.text
        except Exception as e:                  # noqa: BLE001
            log.error("Gemini call failed (%s) — using offline stub", e)
    return _offline_stub(user_text)


# ---------------------------------------------------------------------------
# Offline stub — keyword intent matching. Not clever; only needs to be honest.
# ---------------------------------------------------------------------------

_ON_WORDS = ("turn on", "switch on", "on the", "put on", "light up", "open the light")
_OFF_WORDS = ("turn off", "switch off", "off the", "shut off", "close the light")


def _match_device(text: str) -> str | None:
    text = text.lower()
    best, best_len = None, 0
    for dev_id, d in config.DEVICES.items():
        for alias in d["aliases"] + [dev_id.replace("_", " ")]:
            if alias in text and len(alias) > best_len:
                best, best_len = dev_id, len(alias)
    return best


def _offline_stub(user_text: str) -> str:
    t = user_text.lower().strip()
    device = _match_device(t)

    # fan speed: "set the fan to 60"
    m = re.search(r"\b(\d{1,3})\s*(?:percent|%)?\b", t)
    if device == "fan" and m and any(w in t for w in ("set", "speed", "to")):
        return json.dumps({
            "speech": f"Setting the fan to {int(m.group(1))} percent.",
            "commands": [{"device": "fan", "action": "set", "value": int(m.group(1))}],
        })

    if device and any(w in t for w in _OFF_WORDS):
        return json.dumps({
            "speech": f"Okay, turning the {config.DEVICES[device]['friendly']} off.",
            "commands": [{"device": device, "action": "off"}],
        })

    if device and any(w in t for w in _ON_WORDS):
        if not config.DEVICES[device]["allow_on"]:
            return json.dumps({
                "speech": ("I'm not able to switch the stove on for you. "
                           "If you'd like to cook, please turn it on yourself and "
                           "I'll remind you about it later."),
                "commands": [],
            })
        return json.dumps({
            "speech": f"Okay, turning the {config.DEVICES[device]['friendly']} on.",
            "commands": [{"device": device, "action": "on"}],
        })

    if "medicine" in t or "pill" in t or "tablet" in t:
        if any(w in t for w in ("have i", "did i", "already")):
            return json.dumps({"speech": "Let me check my notes for you.",
                               "commands": [], "log": {}})
        return json.dumps({"speech": "Good, I've written down that you took your medicine.",
                           "commands": [],
                           "log": {"event": "medicine", "value": "taken"}})

    return json.dumps({
        "speech": "I'm not quite sure what you'd like me to do. Could you say that again?",
        "commands": [],
    })

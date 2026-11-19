"""PIPER backend — answering questions about the past from the diary (steps 7-8).

    "Did I take my medicine today?"   "Did I leave the stove on?"

The language model only CHOOSES what to look up, from fixed menus. This file
checks that choice, runs the lookup with diary.py, and builds the spoken answer
from the rows that came back. The model's own sentence is never used for these
answers, so it cannot invent a dose that was never recorded.

    Gemini reply:  {"query": {"kind": "medication", "window": "today"}}
           │
    validate()     kind / window / thing must be on the menus, else refuse
           │
    answer()       diary lookup  ->  fixed sentence (or an honest "no note")
"""
from __future__ import annotations

import config
from diary import KINDS, WINDOWS, Diary, clock

# Things the person can ask about by name: every device, plus medicine.
THINGS = tuple(config.DEVICES) + ("medicine",)

WINDOW_WORDS = {
    "today": "today",
    "yesterday": "yesterday",
    "this_morning": "this morning",
    "this_afternoon": "this afternoon",
    "this_evening": "this evening",
}

NOT_ALLOWED_WINDOW = ("I can only check my notes for today, yesterday, "
                      "this morning, this afternoon or this evening.")
NOT_ALLOWED_THING = "I'm sorry, I don't keep notes about that."


# ---------------------------------------------------------------------------
# Step 7 — the gatekeeper
# ---------------------------------------------------------------------------

def validate(raw) -> tuple[dict | None, str | None]:
    """Check the model's query. Returns (clean_query, None) or (None, reason).

    A missing window means "today" (the safest reading for medicine). Anything
    present but not on a menu is refused, never guessed.
    """
    if not isinstance(raw, dict):
        return None, "query was not an object"

    kind = str(raw.get("kind") or "").strip().lower()
    window = str(raw.get("window") or "today").strip().lower()
    thing = str(raw.get("thing") or "").strip().lower() or None

    if window not in WINDOWS:
        return None, f"window '{window}' not allowed"

    if thing is not None:
        if thing not in THINGS:
            return None, f"thing '{thing}' not allowed"
        if thing == "medicine":            # medicine is looked up by kind
            kind, thing = "medication", None
        elif not kind:
            kind = "device"

    if kind not in KINDS:
        return None, f"kind '{kind}' not allowed"

    return {"kind": kind, "window": window, "thing": thing}, None


def refusal_line(reason: str) -> str:
    if reason.startswith("window"):
        return NOT_ALLOWED_WINDOW
    if reason.startswith("thing"):
        return NOT_ALLOWED_THING
    return "Sorry, could you ask me that again?"


# ---------------------------------------------------------------------------
# Step 8 — the spoken answer
# ---------------------------------------------------------------------------

def answer(diary: Diary, query: dict, home=None) -> str:
    """Look the query up and return one or two plain sentences."""
    kind, window, thing = query["kind"], query["window"], query.get("thing")
    when = WINDOW_WORDS[window]

    if thing:
        return _answer_device(diary, thing, window, when, home)

    rows = diary.events_in(kind, window) or []

    if kind == "medication":
        if not rows:
            # "No note" is NOT "you didn't take it". Never push a second dose.
            return (f"I don't have a note of you taking your medicine {when}. "
                    "If you're not sure, please check your pill box or ask your "
                    "carer before taking any more.")
        times = _join([clock(r[0]) for r in rows])
        return f"Yes. I have a note that you took your medicine {when} at {times}."

    if kind == "alert":
        if not rows:
            return f"Nothing worrying was noted {when}."
        ts, event, value, source = rows[-1]
        extra = f" That was the last of {len(rows)} alerts." if len(rows) > 1 else ""
        return f"At {clock(ts)}, the {_friendly(event)} {_describe(value, source)}.{extra}"

    if kind == "device":
        if not rows:
            return f"Nothing in the house was switched {when}."
        ts, event, value, source = rows[-1]
        return (f"I noted {len(rows)} change{'s' if len(rows) != 1 else ''} {when}. "
                f"The last one was at {clock(ts)}: the {_friendly(event)} "
                f"{_describe(value, source)}.")

    # kind == "note": things worth remembering from the conversation
    if not rows:
        return f"I don't have any notes {when}."
    last = rows[-3:]
    parts = [f"at {clock(ts)}, {event}: {value}" for ts, event, value, _ in last]
    return f"Here is what I noted {when}: " + "; ".join(parts) + "."


def _answer_device(diary, thing, window, when, home) -> str:
    """Up to the last three events for one device, then its live state.

    Listing a few events answers "when did I turn it ON?" as well as "did I
    turn it OFF?", without the model having to pick an action.
    """
    name = _friendly(thing)
    rows = (diary.events_for(thing, window) or [])[-3:]
    if not rows:
        said = f"I don't have any note about the {name} {when}."
    elif len(rows) == 1:
        ts, _, value, source = rows[0]
        said = f"At {clock(ts)} {when}, the {name} {_describe(value, source)}."
    else:
        steps = [f"{_describe(v, src)} at {clock(ts)}" for ts, _, v, src in rows]
        said = f"{when.capitalize()}, the {name} " + ", then ".join(steps) + "."

    # For "did I leave it on?", what matters most is how it is RIGHT NOW,
    # and that comes from the state the house ESP32 itself reported.
    now = _live_state(home, thing)
    if now:
        said += f" Right now it is {now}."
    return said


# ---------------------------------------------------------------------------
# Safety net — used when Gemini is offline or forgets to send a query
# ---------------------------------------------------------------------------

_ASK_WORDS = ("did i", "have i", "had i", "when did", "was the", "did the",
              "did you", "has the", "what happened", "if i took", "if i had",
              "whether i", "did anything", "has anything", "was there", "were there",
              # unsure statements must be CHECKED, never written down as fact
              "didn't i", "haven't i", "i think i took", "i think i had",
              "i think i already", "or not")
_MED_WORDS = ("medicine", "medication", "pill", "tablet", "dose")
_ALERT_WORDS = ("worr", "warning", "alert", "emergenc", "danger", "problem")
# Times PIPER can't look up. Better to say so than to answer about today.
_UNSUPPORTED_TIME = ("last week", "this week", "last month", "days ago", "weeks ago",
                     "monday", "tuesday", "wednesday", "thursday", "friday",
                     "saturday", "sunday", "the other day")


def from_keywords(text: str) -> dict | None:
    """Turn a plain question into a query without an LLM. None if it isn't one."""
    t = text.lower()
    if not any(w in t for w in _ASK_WORDS):
        return None

    if any(w in t for w in _UNSUPPORTED_TIME):
        window = "unsupported"          # validate() refuses it with an honest line
    elif "yesterday" in t or "last night" in t:
        window = "yesterday"
    elif "morning" in t:
        window = "this_morning"
    elif "afternoon" in t:
        window = "this_afternoon"
    elif "evening" in t or "tonight" in t:
        window = "this_evening"
    else:
        window = "today"

    if any(w in t for w in _MED_WORDS):
        return {"kind": "medication", "window": window, "thing": None}

    thing = _match_device(t)
    if thing:
        return {"kind": "device", "window": window, "thing": thing}

    if any(w in t for w in _ALERT_WORDS):
        return {"kind": "alert", "window": window, "thing": None}
    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _match_device(text: str) -> str | None:
    best, best_len = None, 0
    for dev_id, d in config.DEVICES.items():
        for alias in d["aliases"] + [dev_id.replace("_", " ")]:
            if alias in text and len(alias) > best_len:
                best, best_len = dev_id, len(alias)
    return best


def _friendly(event: str) -> str:
    dev = config.DEVICES.get(event)
    return dev["friendly"] if dev else event


def _describe(value: str, source: str | None) -> str:
    """'on (ok)' -> 'was turned on'.  Rule texts are kept as they are."""
    v = (value or "").strip()
    by_rule = {"rule": " automatically", "manual": " by hand",
               "boot": " when the house controller restarted"}.get(source or "", "")
    if v.endswith("(failed)"):
        action = v.split()[0]
        return f"was asked to turn {action}, but it did not respond"
    if v.startswith("on"):
        return f"was turned on{by_rule}"
    if v.startswith("off"):
        return f"was turned off{by_rule}"
    if v.startswith("set"):
        return "was adjusted"
    if v.startswith("cut off"):
        return f"was {v}"
    if v.startswith("warning"):
        return f"set off a {v}"
    return f"had this noted: {v}"


def _live_state(home, thing: str) -> str | None:
    if home is None or not getattr(home, "house_online", False):
        return None
    st = getattr(home, "states", {}).get(thing)
    if st is None or st.state not in ("on", "off"):
        return None
    return st.state


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]

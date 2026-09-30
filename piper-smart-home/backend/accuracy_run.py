"""Measure command execution accuracy — Objective 3 of the report.

    "...controlling selected household appliances such as lighting and fans
     with a command execution accuracy of at least 85%."

Sends a fixed set of phrases to the running server (python server.py) and checks
two things for each one:

  1. Intent   — did the backend choose the right device and action?
  2. Hardware — did the house ESP32 actually end up in that state?

A command only counts as correct if BOTH are true. Intent alone is not
execution; the report's objective is about the light actually changing.

    python accuracy_run.py                       server on this PC
    python accuracy_run.py --host 192.168.1.100  server elsewhere

Run it twice for the report: once with the offline stub, once with Gemini live
(set GEMINI_API_KEY before starting server.py). The difference between the two
paraphrase scores is the argument for using an LLM at all.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# (phrase, expected device, expected action, expected value, category)
#   device None  -> expect NO command (chat, refusal, or unknown device)
CASES = [
    # --- direct commands --------------------------------------------------
    ("turn on the living room light",          "living_light",  "on",  None, "direct"),
    ("turn off the living room light",         "living_light",  "off", None, "direct"),
    ("switch on the bedroom light",            "bedroom_light", "on",  None, "direct"),
    ("switch off the bedroom light",           "bedroom_light", "off", None, "direct"),
    ("turn on the fan",                        "fan",           "on",  None, "direct"),
    ("set the fan to 40",                      "fan",           "set", 40,   "direct"),
    ("turn off the fan",                       "fan",           "off", None, "direct"),
    ("turn off the stove",                     "stove",         "off", None, "direct"),
    # --- paraphrases: how people actually talk -----------------------------
    ("can you put the hall light on please",   "living_light",  "on",  None, "paraphrase"),
    ("it's too dark in the living room",       "living_light",  "on",  None, "paraphrase"),
    ("I'm going to sleep, bedroom light off",  "bedroom_light", "off", None, "paraphrase"),
    ("it's really hot in here",                "fan",           "on",  None, "paraphrase"),
    ("make the fan slower, about half",        "fan",           "set", 50,   "paraphrase"),
    ("the fan is too strong",                  "fan",           "set", None, "paraphrase"),
    ("could you shut the cooker off",          "stove",         "off", None, "paraphrase"),
    ("lights off in the bedroom",              "bedroom_light", "off", None, "paraphrase"),
    # --- must NOT produce a command -----------------------------------------
    ("turn on the stove",                      None, None, None, "safety"),
    ("switch the cooker on for me",            None, None, None, "safety"),
    ("open the garage door",                   None, None, None, "unknown-device"),
    ("how are you today",                      None, None, None, "chat"),
]


def post(url: str, payload: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def judge(case, resp, state) -> tuple[bool, str]:
    phrase, dev, action, value, cat = case
    accepted = resp.get("accepted", [])

    if dev is None:
        if accepted:
            return False, f"expected no command, got {accepted}"
        return True, "correctly refused / no command"

    match = [c for c in accepted if c["device"] == dev]
    if not match:
        return False, f"wrong/no device (got {accepted or 'nothing'})"
    c = match[0]

    if action == "set":
        if c["action"] != "set":
            # "make the fan slower" — any reduction is acceptable if it is a set
            return False, f"expected set, got {c['action']}"
        if value is not None and abs(int(c.get("value", -999)) - value) > 15:
            return False, f"fan value {c.get('value')} not near {value}"
    elif c["action"] != action:
        return False, f"expected {action}, got {c['action']}"

    if not resp.get("all_ok"):
        return False, "house did not confirm (hardware / MQTT failure)"

    # Hardware check: read the state the house node itself reported
    real = state["devices"].get(dev, {})
    want_state = "off" if action == "off" else "on"
    if action == "set" and value == 0:
        want_state = "off"
    if real.get("state") != want_state:
        return False, f"house reports {real.get('state')}, expected {want_state}"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between phrases")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    try:
        h = get(f"{base}/api/health")
    except Exception as e:                                     # noqa: BLE001
        sys.exit(f"  server not reachable at {base} ({e}). Start python server.py first.")
    if not h.get("house_online"):
        sys.exit("  house01 is OFFLINE — power the house ESP32 (or run virtual_house.py) first.")

    print(f"\n  PIPER accuracy run — llm: {h['llm']}   server: {base}\n")
    if h.get("rules") == "on":
        print("  NOTE: automatic rules are ON — a rule could change a device mid-test.")
        print("        For a clean measurement restart the server with  $env:PIPER_RULES=\"off\"\n")
    print(f"  {'#':>2}  {'category':14s} {'result':6s}  phrase")
    print("  " + "-" * 78)

    tally: dict[str, list[int]] = {}
    for i, case in enumerate(CASES, 1):
        try:
            resp = post(f"{base}/api/utterance", {"text": case[0], "source": "accuracy"})
            time.sleep(0.3)
            state = get(f"{base}/api/state")
            ok, why = judge(case, resp, state)
        except Exception as e:                                 # noqa: BLE001
            ok, why = False, f"request failed: {e}"
        tally.setdefault(case[4], [0, 0])
        tally[case[4]][0] += ok
        tally[case[4]][1] += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  {i:>2}  {case[4]:14s} {mark:6s}  {case[0]}")
        if not ok:
            print(f"      {'':14s}         └ {why}")
        time.sleep(args.delay)

    total_ok = sum(v[0] for v in tally.values())
    total = sum(v[1] for v in tally.values())
    print("\n  " + "-" * 78)
    for cat, (k, n) in tally.items():
        print(f"  {cat:16s} {k:>2}/{n:<2}  {100*k/n:5.1f}%")
    pct = 100 * total_ok / total
    verdict = "MEETS" if pct >= 85 else "BELOW"
    print(f"\n  OVERALL          {total_ok:>2}/{total:<2}  {pct:5.1f}%   → {verdict} the 85% objective")
    print(f"  (llm: {h['llm']} — every request is also in backend/test_log.csv)\n")


if __name__ == "__main__":
    main()

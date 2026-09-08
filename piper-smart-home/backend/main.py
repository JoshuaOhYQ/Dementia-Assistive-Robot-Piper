"""PIPER backend — smart-home slice, runnable on its own.

This is the conversation branch and the home-monitoring branch of the report's
flow chart, wired to MQTT. Speech-to-text and text-to-speech are stubbed with
the keyboard and the console so this slice can be developed and demonstrated
before Whisper and Piper TTS are integrated. When they are, replace
`input()` with the Whisper transcript and `print()` with the Piper WAV.

Run:
    python main.py            interactive
    python main.py --demo     scripted demo, no typing (good for a video)
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

import command_bridge
import config
import gemini_client
from diary import Diary
from rules import RuleEngine
from smart_home import SmartHome

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)-18s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("piper.main")

BANNER = r"""
   ___ ___ ___ ___ ___
  | _ \_ _| _ \ __| _ \    Dementia Assistive Robot
  |  _/| ||  _/ _||   /    Smart Home / MQTT subsystem
  |_| |___|_| |___|_|_\
"""


def speak(text: str):
    """Stand-in for Piper TTS."""
    print(f"\n  PIPER: {text}\n")


def alert(text: str):
    """Stand-in for the Telegram Bot API call."""
    print(f"\n  [TELEGRAM -> caregiver] {text}\n")


def handle_utterance(text: str, home: SmartHome, diary: Diary) -> str:
    """One pass of the conversation branch: STT text in, spoken reply out."""
    diary_lines = Diary.as_lines(diary.today(limit=12))
    prompt = command_bridge.build_system_prompt(home, diary_lines)

    raw = gemini_client.ask(prompt, text)
    reply = command_bridge.parse_llm_reply(raw)

    # Memory questions the stub cannot answer are answered from the diary here,
    # so the feature works even with no LLM at all.
    if not reply.get("commands") and any(w in text.lower() for w in ("have i", "did i")):
        for kw in ("medicine", "pill", "stove", "light", "fan"):
            if kw in text.lower():
                row = diary.last(kw)
                if row:
                    ts, event, value, _ = row
                    return (f"Yes — at {time.strftime('%H:%M', time.localtime(ts))} "
                            f"I recorded {event}: {value}.")
                return f"I don't have a record of that today."

    return command_bridge.execute(home, reply, diary=diary)


def monitoring_loop(engine: RuleEngine, stop: threading.Event):
    """The idle / home-monitoring branch: one pass every 5 seconds."""
    while not stop.is_set():
        try:
            engine.tick()
        except Exception:                       # noqa: BLE001
            log.exception("rule engine error")
        stop.wait(5.0)


DEMO_SCRIPT = [
    "Piper, turn on the living room light",
    "set the fan to 60",
    "turn on the stove",                       # must be refused
    "I have taken my medicine",
    "have I taken my medicine today?",
    "turn off the living room light",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="run the scripted demo")
    args = ap.parse_args()

    print(BANNER)
    print(f"  broker : {config.MQTT_HOST}:{config.MQTT_PORT}")
    print(f"  topics : {config.TOPIC_ROOT}/cmd/+  ->  {config.TOPIC_ROOT}/state/+")
    print(f"  gemini : {'live' if gemini_client._model else 'offline stub'}\n")

    diary = Diary()
    home = SmartHome()
    if not home.connect():
        print("  !! broker not reachable. Is Mosquitto running?")
        print("     Windows:  net start mosquitto")
        print("     or point at a public broker:  set PIPER_MQTT_HOST=broker.emqx.io\n")

    # Give retained state a moment to arrive before we start judging the house.
    time.sleep(1.5)
    status = "ONLINE" if home.house_online else "OFFLINE — start the ESP32 or virtual_house.py"
    print(f"  house  : {status}\n")

    engine = RuleEngine(home, diary, speak=speak, alert=alert)
    stop = threading.Event()
    t = threading.Thread(target=monitoring_loop, args=(engine, stop), daemon=True)
    t.start()

    try:
        if args.demo:
            for line in DEMO_SCRIPT:
                print(f"  YOU  : {line}")
                speak(handle_utterance(line, home, diary))
                time.sleep(1.5)
        else:
            print("  Type what the person says. 'state' prints the house, "
                  "'diary' prints today, Ctrl-C to quit.\n")
            while True:
                try:
                    line = input("  YOU  : ").strip()
                except EOFError:
                    break
                if not line:
                    continue
                if line == "state":
                    print(f"  {home.describe()}")
                    continue
                if line == "diary":
                    for l in Diary.as_lines(diary.today()):
                        print(f"   {l}")
                    continue
                speak(handle_utterance(line, home, diary))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        home.stop()
        print("\n  stopped.")


if __name__ == "__main__":
    sys.exit(main())

"""PIPER backend — configuration and device registry.

Everything the smart-home layer needs to know about the house lives here.
The registry is the security boundary: if a device is not in this file, the
LLM cannot touch it, no matter what it emits.
"""
import os

# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------
# Local Mosquitto by default. Override with environment variables to point the
# same code at a public broker for Wokwi testing:
#   set PIPER_MQTT_HOST=broker.emqx.io
MQTT_HOST = os.getenv("PIPER_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("PIPER_MQTT_PORT", "1883"))
MQTT_USER = os.getenv("PIPER_MQTT_USER", "")
MQTT_PASS = os.getenv("PIPER_MQTT_PASS", "")

TOPIC_ROOT = os.getenv("PIPER_TOPIC_ROOT", "piper")
BACKEND_ID = "backend"
HOUSE_NODE = "house01"

# How long we wait for the ESP32 to confirm a command before we admit failure.
COMMAND_ACK_TIMEOUT_S = 3.0

# ---------------------------------------------------------------------------
# Device registry — must match the table in the firmware
# ---------------------------------------------------------------------------
SWITCH = "switch"
DIMMABLE = "dimmable"

DEVICES = {
    "living_light": {
        "type": SWITCH,
        "friendly": "living room light",
        "aliases": ["living room light", "hall light", "sitting room light", "lounge light"],
        "actions": ["on", "off", "toggle"],
        "allow_on": True,
    },
    "bedroom_light": {
        "type": SWITCH,
        "friendly": "bedroom light",
        "aliases": ["bedroom light", "room light", "night light"],
        "actions": ["on", "off", "toggle"],
        "allow_on": True,
    },
    "fan": {
        "type": DIMMABLE,
        "friendly": "fan",
        "aliases": ["fan", "ceiling fan", "living room fan"],
        "actions": ["on", "off", "toggle", "set"],
        "allow_on": True,
    },
    "stove": {
        "type": SWITCH,
        "friendly": "stove",
        "aliases": ["stove", "cooker", "hob", "gas"],
        # NOTE: "on" is deliberately absent. A language model must never be able
        # to energise a heating element. Turning the stove on stays a human act.
        "actions": ["off"],
        "allow_on": False,
    },
}

SENSORS = {
    "living_occupancy": {"friendly": "living room occupancy"},
    "stove_on_seconds": {"friendly": "stove on-time"},
}

MAX_COMMANDS_PER_UTTERANCE = 3

# ---------------------------------------------------------------------------
# Home-monitoring rules
# ---------------------------------------------------------------------------
RULES = {
    "night_start_hour": 22,          # 22:00
    "night_end_hour": 6,             # 06:00
    "night_light_auto_off_s": 300,   # turn the path light off 5 min after the room empties
    "stove_warn_after_s": 900,       # 15 min unattended -> warn
    "stove_cutoff_after_s": 1200,    # 20 min unattended -> cut power + Telegram
    "empty_room_light_off_s": 600,   # light on, nobody there for 10 min -> off
}

DB_PATH = os.getenv("PIPER_DB", "piper_diary.db")

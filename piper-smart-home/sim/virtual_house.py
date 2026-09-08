"""PIPER — virtual smart-home node.

A faithful software twin of piper_house_node.ino: same topics, same payloads,
same LWT, same safety interlock. Run this instead of (or alongside) the second
ESP32 so the whole smart-home feature can be built and demonstrated with zero
extra hardware.

    pip install paho-mqtt
    python virtual_house.py

Keys while running:
    o   toggle living-room occupancy (stands in for the PIR)
    s   toggle the stove ON by hand (the "human at the cooker" action)
    p   print the current house state
    q   quit
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("PIPER_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("PIPER_MQTT_PORT", "1883"))
TOPIC_ROOT = os.getenv("PIPER_TOPIC_ROOT", "piper")
NODE_ID = "house01"

DEVICES = {
    "living_light":  {"type": "switch",   "allow_on": True},
    "bedroom_light": {"type": "switch",   "allow_on": True},
    "fan":           {"type": "dimmable", "allow_on": True},
    "stove":         {"type": "switch",   "allow_on": False},
}

state = {d: {"state": "off", "value": 0, "req_id": "boot", "source": "boot"}
         for d in DEVICES}
occupancy = True
stove_on_since = 0.0
_lock = threading.Lock()

AVAIL = f"{TOPIC_ROOT}/availability/{NODE_ID}"


def publish_state(client, dev):
    with _lock:
        s = dict(state[dev])
    s["ts"] = int(time.time())
    client.publish(f"{TOPIC_ROOT}/state/{dev}", json.dumps(s), qos=1, retain=True)
    icon = "*" if s["state"] == "on" else "."
    print(f"  [{icon}] {dev:14s} {s['state']:3s} {s['value']:3d}%   ({s['source']})")


def publish_sensor(client, sensor, value):
    client.publish(f"{TOPIC_ROOT}/sensor/{sensor}",
                   json.dumps({"value": value, "ts": int(time.time())}),
                   qos=0, retain=True)


def on_connect(client, userdata, flags, rc):
    print(f"  virtual house connected to {MQTT_HOST}:{MQTT_PORT} (rc={rc})")
    client.publish(AVAIL, "online", qos=1, retain=True)
    client.subscribe(f"{TOPIC_ROOT}/cmd/+", qos=1)
    for d in DEVICES:
        publish_state(client, d)
    publish_sensor(client, "living_occupancy", occupancy)


def on_message(client, userdata, msg):
    global stove_on_since
    dev = msg.topic.rsplit("/", 1)[-1]
    if dev not in DEVICES:
        print(f"  ignored command for unknown device '{dev}'")
        return
    try:
        cmd = json.loads(msg.payload.decode())
    except ValueError:
        print(f"  bad JSON on {msg.topic}")
        return

    action = str(cmd.get("action", "")).lower()
    meta = DEVICES[dev]

    with _lock:
        cur = state[dev]
        new_state, new_value = cur["state"], cur["value"]

        if action == "on":
            new_state = "on"
            if meta["type"] == "dimmable" and new_value == 0:
                new_value = 100
        elif action == "off":
            new_state = "off"
        elif action == "toggle":
            new_state = "off" if cur["state"] == "on" else "on"
            if new_state == "on" and meta["type"] == "dimmable" and new_value == 0:
                new_value = 100
        elif action == "set" and meta["type"] == "dimmable":
            new_value = max(0, min(100, int(cmd.get("value", 100))))
            new_state = "on" if new_value > 0 else "off"
        else:
            print(f"  action '{action}' not valid for {dev}")
            return

        # Same safety interlock as the firmware — enforced at the device, not
        # only in the backend.
        if new_state == "on" and not meta["allow_on"]:
            print(f"  REFUSED: {dev} may not be switched on remotely")
            publish_state(client, dev)
            return

        cur["state"] = new_state
        cur["value"] = new_value if meta["type"] == "dimmable" else (100 if new_state == "on" else 0)
        cur["req_id"] = str(cmd.get("req_id", "none"))
        cur["source"] = str(cmd.get("source", "unknown"))

    if dev == "stove":
        stove_on_since = time.time() if new_state == "on" else 0.0

    publish_state(client, dev)


def sensor_loop(client, stop):
    while not stop.is_set():
        publish_sensor(client, "living_occupancy", occupancy)
        secs = int(time.time() - stove_on_since) if stove_on_since else 0
        publish_sensor(client, "stove_on_seconds", secs)
        stop.wait(5.0)


def main():
    global occupancy, stove_on_since

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,          # paho 2.x
                             client_id=f"piper-virtual-{NODE_ID}")
    except AttributeError:
        client = mqtt.Client(client_id=f"piper-virtual-{NODE_ID}")      # paho 1.x
    client.will_set(AVAIL, "offline", qos=1, retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    stop = threading.Event()
    threading.Thread(target=sensor_loop, args=(client, stop), daemon=True).start()

    interactive = sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        print("\n  keys:  o = toggle occupancy   s = stove on (by hand)   "
              "p = print state   q = quit\n")
    else:
        print("\n  running headless (no terminal attached) — Ctrl-C to stop\n")

    try:
        while True:
            if not interactive:
                # Piped or backgrounded: stay alive serving MQTT rather than
                # exiting on EOF.
                line = sys.stdin.readline()
                if line == "":
                    time.sleep(3600)
                    continue
                key = line.strip().lower()
            else:
                key = input().strip().lower()
            if key == "q":
                break
            elif key == "o":
                occupancy = not occupancy
                publish_sensor(client, "living_occupancy", occupancy)
                print(f"  occupancy = {occupancy}")
            elif key == "s":
                with _lock:
                    state["stove"]["state"] = "on"
                    state["stove"]["value"] = 100
                    state["stove"]["source"] = "manual"
                    state["stove"]["req_id"] = "manual"
                stove_on_since = time.time()
                publish_state(client, "stove")
                print("  stove switched on by hand (the AI cannot do this)")
            elif key == "p":
                for d in DEVICES:
                    publish_state(client, d)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        client.publish(AVAIL, "offline", qos=1, retain=True)
        time.sleep(0.3)
        client.loop_stop()
        client.disconnect()
        print("  virtual house stopped.")


if __name__ == "__main__":
    sys.exit(main())

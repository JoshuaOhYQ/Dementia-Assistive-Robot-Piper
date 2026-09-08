"""Diagnostic: is the broker's WebSocket listener actually delivering messages?

Connects to port 9001 over WebSockets using paho — the same transport the browser
dashboard uses, but with no browser involved. If this sees messages and the
dashboard does not, the fault is in the page. If neither sees messages while
`mosquitto_sub -t "piper/#"` on port 1883 does, the fault is in the broker's
WebSocket listener.

    python ws_check.py
"""
import sys
import time

import paho.mqtt.client as mqtt

HOST = "localhost"
PORT = 9001
ROOT = "piper"

count = 0


def on_connect(client, userdata, flags, rc):
    print(f"  connected over websockets, rc={rc}")
    client.subscribe(f"{ROOT}/#", qos=1)
    print(f"  subscribed to {ROOT}/#  — waiting 15 s for messages...\n")


def on_subscribe(client, userdata, mid, granted_qos):
    print(f"  SUBACK received, granted qos {granted_qos}\n")


def on_message(client, userdata, msg):
    global count
    count += 1
    print(f"  [{count:3d}] {msg.topic}  {msg.payload.decode(errors='replace')[:80]}")


try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                         client_id="piper-wscheck", transport="websockets")
except AttributeError:
    client = mqtt.Client(client_id="piper-wscheck", transport="websockets")

client.on_connect = on_connect
client.on_subscribe = on_subscribe
client.on_message = on_message

print(f"\n  connecting to ws://{HOST}:{PORT} ...")
try:
    client.connect(HOST, PORT, keepalive=30)
except Exception as e:                                    # noqa: BLE001
    print(f"  CONNECT FAILED: {e}")
    print("  → nothing is listening for websockets on that port.")
    sys.exit(1)

client.loop_start()
time.sleep(15)
client.loop_stop()
client.disconnect()

print()
if count:
    print(f"  RESULT: {count} messages over websockets. The broker is fine —")
    print("          the problem is in the browser page.")
else:
    print("  RESULT: 0 messages over websockets, but mosquitto_sub on 1883 sees")
    print("          traffic. The broker's websocket listener is not delivering,")
    print("          or 9001 belongs to a different broker process.")

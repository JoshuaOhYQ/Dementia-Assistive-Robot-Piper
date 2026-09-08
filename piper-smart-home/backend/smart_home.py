"""PIPER backend — smart home MQTT client.

This is the only module in the system that is allowed to publish commands.
It maintains a live mirror of the house state (fed by retained state messages
from the ESP32) so the rest of the backend can ask "is the stove on?" without
touching the network.

Usage:
    from smart_home import SmartHome
    home = SmartHome()
    home.connect()
    ok, msg = home.command("living_light", "on", source="voice")
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import paho.mqtt.client as mqtt

import config
from mqtt_compat import make_client

log = logging.getLogger("piper.smart_home")


@dataclass
class DeviceState:
    device_id: str
    state: str = "unknown"       # "on" | "off" | "unknown"
    value: int = 0               # 0-100
    source: str = ""
    req_id: str = ""
    updated_at: float = 0.0
    # When did this device last change to its current state? Used by the rules.
    changed_at: float = field(default_factory=time.time)


class SmartHome:
    def __init__(self, on_state_change: Optional[Callable[[DeviceState], None]] = None):
        self.states: dict[str, DeviceState] = {
            d: DeviceState(d) for d in config.DEVICES
        }
        self.sensors: dict[str, dict] = {}
        self.house_online = False
        self._on_state_change = on_state_change
        self._acks: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

        self.client = make_client(f"piper-backend-{uuid.uuid4().hex[:6]}")
        if config.MQTT_USER:
            self.client.username_pw_set(config.MQTT_USER, config.MQTT_PASS)

        will = f"{config.TOPIC_ROOT}/availability/{config.BACKEND_ID}"
        self.client.will_set(will, "offline", qos=1, retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # -- lifecycle ---------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> bool:
        log.info("connecting to broker %s:%s", config.MQTT_HOST, config.MQTT_PORT)
        self.client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
        self.client.loop_start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.client.is_connected():
                return True
            time.sleep(0.1)
        log.warning("broker not reachable within %.1fs (will keep retrying)", timeout)
        return False

    def stop(self):
        self.client.publish(
            f"{config.TOPIC_ROOT}/availability/{config.BACKEND_ID}",
            "offline", qos=1, retain=True,
        )
        self.client.loop_stop()
        self.client.disconnect()

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("broker refused connection, rc=%s", rc)
            return
        log.info("connected to broker")
        root = config.TOPIC_ROOT
        client.subscribe([(f"{root}/state/+", 1),
                          (f"{root}/sensor/+", 0),
                          (f"{root}/availability/+", 1),
                          (f"{root}/event/+", 1)])
        client.publish(f"{root}/availability/{config.BACKEND_ID}",
                       "online", qos=1, retain=True)

    def _on_disconnect(self, client, userdata, rc):
        # paho reconnects automatically because we used connect_async + loop_start
        log.warning("disconnected from broker (rc=%s), retrying", rc)
        self.house_online = False

    def _on_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            kind, name = parts[1], parts[2]
        except IndexError:
            return

        if kind == "availability":
            if name == config.HOUSE_NODE:
                self.house_online = msg.payload.decode(errors="ignore") == "online"
                log.info("house node is %s", "ONLINE" if self.house_online else "OFFLINE")
            return

        try:
            data = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            log.warning("non-JSON payload on %s", msg.topic)
            return

        if kind == "state":
            self._handle_state(name, data)
        elif kind == "sensor":
            self.sensors[name] = data
        elif kind == "event":
            log.info("event from %s: %s", name, data)

    def _handle_state(self, device_id: str, data: dict):
        with self._lock:
            st = self.states.get(device_id)
            if st is None:
                log.warning("state for unregistered device '%s' ignored", device_id)
                return
            new_state = str(data.get("state", "unknown"))
            if new_state != st.state:
                st.changed_at = time.time()
            st.state = new_state
            st.value = int(data.get("value", 0))
            st.source = str(data.get("source", ""))
            st.req_id = str(data.get("req_id", ""))
            st.updated_at = time.time()

            ev = self._acks.get(st.req_id)

        log.info("state %s = %s (%s%%) via %s", device_id, st.state, st.value, st.source)
        if ev:
            ev.set()
        if self._on_state_change:
            self._on_state_change(st)

    # -- the public API ----------------------------------------------------

    def command(self, device_id: str, action: str, value: int | None = None,
                source: str = "voice", wait: bool = True) -> tuple[bool, str]:
        """Validate, publish, and wait for the device to confirm.

        Returns (ok, human_readable_message). The message is safe to speak.
        """
        dev = config.DEVICES.get(device_id)
        if dev is None:
            return False, f"I don't know a device called {device_id}."

        action = action.lower().strip()
        if action not in dev["actions"]:
            if action == "on" and not dev["allow_on"]:
                return False, (f"For your safety I can only switch the "
                               f"{dev['friendly']} off, not on.")
            return False, f"I can't {action} the {dev['friendly']}."

        if not self.client.is_connected():
            return False, "I can't reach the home system right now."
        if not self.house_online:
            return False, f"The {dev['friendly']} isn't responding at the moment."

        payload = {
            "action": action,
            "req_id": uuid.uuid4().hex[:6],
            "source": source,
            "ts": int(time.time()),
        }
        if action == "set":
            if dev["type"] != config.DIMMABLE:
                return False, f"The {dev['friendly']} doesn't have a speed setting."
            payload["value"] = max(0, min(100, int(value if value is not None else 100)))

        ev = threading.Event()
        with self._lock:
            self._acks[payload["req_id"]] = ev

        topic = f"{config.TOPIC_ROOT}/cmd/{device_id}"
        # retain=False — a retained command would replay on every reconnect
        # and switch appliances on by itself.
        self.client.publish(topic, json.dumps(payload), qos=1, retain=False)
        log.info("cmd -> %s %s", topic, payload)

        if not wait:
            return True, f"Okay, {dev['friendly']}."

        confirmed = ev.wait(config.COMMAND_ACK_TIMEOUT_S)
        with self._lock:
            self._acks.pop(payload["req_id"], None)

        if not confirmed:
            return False, (f"I sent the command but the {dev['friendly']} "
                           f"didn't confirm. Please check it.")

        st = self.states[device_id]
        if action == "set":
            return True, f"The {dev['friendly']} is now at {st.value} percent."
        return True, f"The {dev['friendly']} is now {st.state}."

    # -- convenience for the LLM prompt and the diary ----------------------

    def snapshot(self) -> dict:
        """A compact picture of the house, small enough to paste into a prompt."""
        return {
            "house_online": self.house_online,
            "devices": {
                d: {"state": s.state, "value": s.value,
                    "for_seconds": int(time.time() - s.changed_at)}
                for d, s in self.states.items()
            },
            "sensors": {k: v.get("value") for k, v in self.sensors.items()},
        }

    def describe(self) -> str:
        """One-line English summary, used inside the Gemini system prompt."""
        if not self.house_online:
            return "The home controller is offline; appliance states are unknown."
        bits = []
        for d, s in self.states.items():
            f = config.DEVICES[d]["friendly"]
            if config.DEVICES[d]["type"] == config.DIMMABLE and s.state == "on":
                bits.append(f"{f} on at {s.value}%")
            else:
                bits.append(f"{f} {s.state}")
        occ = self.sensors.get("living_occupancy", {}).get("value")
        if occ is not None:
            bits.append("someone is in the living room" if occ
                        else "the living room is empty")
        return "; ".join(bits) + "."

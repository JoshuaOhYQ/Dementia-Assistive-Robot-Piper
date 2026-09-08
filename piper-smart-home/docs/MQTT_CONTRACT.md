# PIPER — Smart Home MQTT Contract v1.0

This is the single source of truth for how the Python backend (robot brain) and the
smart-home ESP32 talk to each other. Firmware, backend and simulators all implement
this document. Change this file first, then change the code.

---

## 1. Roles

| Node | Role in MQTT | Runs on |
|---|---|---|
| `backend` | Publisher of commands, subscriber of state | Python on the PC |
| `house01` | Subscriber of commands, publisher of state | Smart-home ESP32 (or Wokwi, or the virtual-house dashboard) |
| `robot01` | Publisher of events only (optional) | Robot ESP32 |
| broker | Mosquitto on the PC, port 1883 | PC |

Only **one** node owns a physical output. `house01` owns every actuator. The backend never
assumes a light is on because it sent a command — it waits for the state message. This is
the difference between an open-loop and a closed-loop system, and it is worth one paragraph
in the report.

---

## 2. Topic tree

```
piper/
├── cmd/<device_id>            backend  -> house   QoS 1, retain=false
├── state/<device_id>          house    -> backend QoS 1, retain=TRUE
├── sensor/<sensor_id>         house    -> backend QoS 0, retain=true
├── availability/<node_id>     any node -> all     QoS 1, retain=TRUE  (LWT)
└── event/<node_id>            any node -> backend QoS 1, retain=false
```

Rules of thumb used here (defensible in a viva):

* **Commands are never retained.** A retained command would re-fire every time the ESP32
  reconnects — the light would switch on by itself at 3 a.m. This is the classic MQTT
  smart-home bug.
* **State is always retained.** When the backend restarts it immediately learns the state
  of the whole house without polling anything.
* **QoS 1** on commands and state: at-least-once. Duplicates are harmless because every
  command is idempotent (`on` twice = on).
* **Availability uses the Last Will and Testament.** The ESP32 registers `offline` as its
  will at connect time; the broker publishes it automatically if the ESP32 drops. That is
  how the robot knows to say "I cannot reach the lights right now" instead of lying.

---

## 3. Device registry

| `device_id` | Description | Type | ESP32 GPIO | Actions |
|---|---|---|---|---|
| `living_light` | Living room light | switch | 26 | `on`, `off`, `toggle` |
| `bedroom_light` | Bedroom light | switch | 27 | `on`, `off`, `toggle` |
| `fan` | Living room fan | dimmable | 25 (LEDC PWM) | `on`, `off`, `set` (`value` 0–100) |
| `stove` | Stove power relay | switch | 33 | `on`, `off` (robot may only send `off` — safety) |

| `sensor_id` | Description | ESP32 GPIO | Payload |
|---|---|---|---|
| `living_occupancy` | PIR / occupancy in living room | 14 (input) | `{"value": true/false}` |
| `stove_on_seconds` | How long the stove has been on | derived on house node | `{"value": <int seconds>}` |

`stove` is deliberately **off-only** from the AI path. An LLM is never allowed to energise a
heating element. Turning the stove *on* is a human-only action (physical switch).

---

## 4. Payloads

### 4.1 Command — `piper/cmd/<device_id>`

```json
{
  "action": "on",
  "value": 80,
  "req_id": "a3f1c9",
  "source": "voice",
  "ts": 1757300000
}
```

| Field | Required | Notes |
|---|---|---|
| `action` | yes | `on` \| `off` \| `toggle` \| `set` |
| `value` | only for `set` | integer 0–100 (fan speed %) |
| `req_id` | yes | 6 hex chars; echoed back in the state message so the backend can confirm *this* command landed |
| `source` | yes | `voice` \| `rule` \| `manual` — lets the diary say *why* something happened |
| `ts` | yes | Unix seconds, from the PC (ESP32 clock is not trusted) |

### 4.2 State — `piper/state/<device_id>` (retained)

```json
{
  "state": "on",
  "value": 80,
  "req_id": "a3f1c9",
  "source": "voice",
  "ts": 1757300001
}
```

`req_id` is `"boot"` when the node publishes its start-up state, and `"manual"` if the state
changed from a physical button.

### 4.3 Availability — `piper/availability/house01` (retained, LWT)

Plain text, not JSON: `online` or `offline`.

### 4.4 Sensor — `piper/sensor/<sensor_id>` (retained)

```json
{ "value": true, "ts": 1757300001 }
```

### 4.5 Event — `piper/event/<node_id>`

```json
{ "event": "fall_detected", "detail": "living_room", "ts": 1757300001 }
```

---

## 5. What Gemini is allowed to return

The LLM never writes MQTT. It returns **one JSON object** and the backend translates it.
This keeps the model on a short leash and makes the system testable without the API.

```json
{
  "speech": "Okay, I've turned the living room light on for you.",
  "commands": [
    { "device": "living_light", "action": "on" }
  ],
  "log": { "event": "living_light", "value": "on" }
}
```

Backend validation before anything is published (`command_bridge.py`):

1. `device` must exist in the registry — unknown device is dropped, not published.
2. `action` must be in that device's allowed action list.
3. `stove` + `on` is rejected outright.
4. `value` is clamped to 0–100 and only accepted for `dimmable` devices.
5. Max 3 commands per utterance (stops a runaway model flipping the whole house).

Anything rejected is logged and the robot says a safe fallback line instead.

---

## 6. Automatic rules (home-monitoring branch)

These run in the backend, not in the ESP32, so they can use the diary and the clock.

| Rule | Trigger | Action |
|---|---|---|
| Night path lighting | occupancy true in living room between 22:00–06:00 and `living_light` is off | `living_light` on, `source: "rule"`, auto-off after 5 min of no occupancy |
| Stove left unattended | `stove` state on for > 15 min with occupancy false | speak a warning; if still on after another 5 min, publish `stove` off and send Telegram |
| Empty-room light | `living_light` on and occupancy false for > 10 min | `living_light` off, `source: "rule"` |

Every rule action writes a row into the SQLite diary, which is what makes
"did I leave the stove on?" answerable later.

---

## 7. Failure behaviour

| Failure | Detected by | Robot behaviour |
|---|---|---|
| ESP32 offline | `availability/house01` = `offline` | "I can't reach the lights at the moment." No command is published. |
| Broker down | paho `on_disconnect` | Same message; backend retries with exponential backoff |
| Command sent, no state within 3 s | timeout on `req_id` | Retry once, then tell the user it did not respond |
| Wi-Fi drop on ESP32 | reconnect loop in firmware | Republishes all retained state on reconnect |

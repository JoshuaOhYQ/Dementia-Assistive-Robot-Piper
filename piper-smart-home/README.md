# PIPER — Smart Home / MQTT Subsystem

Implements Section 3.2 (Smart Home Automation), 3.4.2 and 3.5.2 of the IDP1 G2 final
report: the path from *"Piper, turn off the light"* to a relay actually clicking, plus
the automatic home-monitoring rules.

Built to run **with no hardware at all**, then with **one ESP32**, then with two —
without changing a line of the protocol.

---

## The three ways to run this

| # | You have | House node is | Broker | Use it for |
|---|---|---|---|---|
| 1 | nothing | `sim/virtual_house.py` | Mosquitto on your PC | writing and testing backend logic today |
| 2 | a browser | `sim/wokwi/` (simulated ESP32) | `broker.emqx.io` (public) | proving the *firmware* works with no board |
| 3 | 1 ESP32 | the real board flashed with the sketch | Mosquitto on your PC | the demo and the report's test results |

Your one ESP32 should become the **smart-home node**, not the robot node. The robot node
needs the mmWave sensor, the I²S mic and the amplifier before it does anything useful;
the house node needs four LEDs. So flash the house node now, keep the board for the
smart-home demo, and add the robot ESP32 when the rest of the BOM arrives.

---

## Files

```
docs/MQTT_CONTRACT.md          <- read this first. Topics, payloads, safety rules.
mosquitto.conf                 broker config (fixes the "only listens on localhost" trap)

firmware/piper_house_node/
  piper_house_node.ino         smart-home ESP32: subscribes to commands, drives outputs

backend/
  config.py                    broker settings + the device registry (the security boundary)
  smart_home.py                the MQTT client. The only module allowed to publish commands.
  command_bridge.py            Gemini prompt builder + hard validator + executor
  rules.py                     home-monitoring branch (night lighting, stove, empty rooms)
  diary.py                     SQLite event store (report section 3.4.6)
  gemini_client.py             Gemini wrapper with an offline keyword stub
  main.py                      runnable CLI: type what the person says
  test_smart_home.py           19 end-to-end tests — evidence for the testing section

sim/
  virtual_house.py             software twin of the ESP32. Same topics, same interlocks.
  dashboard.html               live browser view of the house (MQTT over WebSockets)
  wokwi/                       diagram.json + instructions for the browser simulator
```

---

## Quick start (30 minutes, no hardware)

**1. Install Mosquitto**

Windows: download from <https://mosquitto.org/download/>. Then replace the installed
`mosquitto.conf` with the one in this repo and start it from an Administrator terminal:

```
"C:\Program Files\mosquitto\mosquitto.exe" -c "C:\Program Files\mosquitto\mosquitto.conf" -v
```

Leave that window open — the `-v` log is the best debugging tool you will have, and a
screenshot of it belongs in the report.

**2. Install the Python dependency**

```
pip install paho-mqtt
```

**3. Start the virtual house** (terminal 2)

```
cd sim
python virtual_house.py
```

**4. Start the backend** (terminal 3)

```
cd backend
python main.py
```

Type `turn on the living room light`. Terminal 2 prints `[*] living_light on 100%`.
That is a complete command round trip — publish, subscribe, act, confirm.

Type `turn on the stove`. It refuses, twice over, in two independent places.

**5. Open `sim/dashboard.html`** in a browser for the visual version. It connects on
`ws://localhost:9001`, which the supplied `mosquitto.conf` enables.

**6. Run the tests**

```
cd backend
python test_smart_home.py
```

19 tests, ~19 seconds. They start the virtual house themselves.

---

## Then: put it on the real ESP32

1. Arduino IDE → Library Manager → install **PubSubClient** and **ArduinoJson**.
2. Open `firmware/piper_house_node/piper_house_node.ino`.
3. Set `WIFI_SSID`, `WIFI_PASS`, and `MQTT_HOST` to your PC's LAN IP (`ipconfig`,
   the IPv4 address of your Wi-Fi adapter — **not** `127.0.0.1`; the ESP32 has its own
   idea of what localhost means).
4. Wire four LEDs with 220 Ω resistors to GPIO 26 / 27 / 25 / 33, and a button
   (with a 10 kΩ pulldown) to GPIO 14.
5. Flash, open the serial monitor at 115200. You should see `connected` then four
   `[STATE]` lines.
6. Stop `virtual_house.py` and run only `main.py`. Everything else is identical.

Common first-run problem: Windows Firewall blocks inbound 1883. Allow
`mosquitto.exe` on Private networks, or the ESP32 will connect to Wi-Fi and then
sit there failing with `rc=-2`.

---

## Design decisions worth writing up

These are the parts a marker will ask about.

**Commands are not retained; state is.** A retained command message replays to every
client that subscribes, including the ESP32 after a reboot — so a retained
`{"action":"on"}` would switch the light on by itself after every power cut. Retained
*state*, on the other hand, means the backend knows the whole house the instant it
starts, with no polling.

**Last Will and Testament.** The ESP32 tells the broker at connect time: "if I vanish,
publish `offline` on my behalf." So when the house node loses power, PIPER says *"I
can't reach the lights at the moment"* instead of confidently lying to someone with
dementia. For this user group that distinction is a safety property, not a nicety.

**Closed loop, not open loop.** The backend never says "done" because it sent a
command. It waits up to 3 seconds for the device's own state message carrying the
matching `req_id`. No confirmation, no claim of success.

**The LLM is treated as an untrusted input.** Gemini returns JSON; `command_bridge.py`
checks the device exists, the action is permitted, the value is in range, and the count
is capped, before anything is published. A hallucinated `{"device": "garage_door"}` is
dropped with a log line, not forwarded.

**Two independent stove interlocks.** The backend registry has no `on` action for the
stove, *and* the firmware refuses `on` for any device with `allow_on = false` and
re-publishes its true state. Either one alone would work; both together mean a bug in
one does not energise a heating element. This is defence in depth and it is exactly the
kind of thing the safety section of the report should contain.

**Rules live in the backend, not the firmware.** The night-lighting and unattended-stove
rules need the wall clock and the diary. Putting them on the ESP32 would mean an RTC, a
filesystem, and a firmware reflash every time a threshold changes.

---

## Known limits (say these in the report; they read as rigour, not excuses)

* The simulation validates protocol, logic and timing. It cannot validate relay
  switching, mains isolation, PIR false-positive rates, Wi-Fi range or power draw.
  Those need bench tests once the parts arrive.
* `allow_anonymous true` is a development setting. The report claims local, private
  processing; an open broker on the LAN undercuts that claim. Turn on the password
  file before the demo and mention it in Section 2.5.
* The public broker used for Wokwi is unencrypted and world-readable. Never point a
  real relay at it — use a unique `TOPIC_ROOT` and only for simulation.
* Occupancy is currently a single binary input. The report's night-lighting scenario
  really wants per-room presence, which the LD2450 could provide later.

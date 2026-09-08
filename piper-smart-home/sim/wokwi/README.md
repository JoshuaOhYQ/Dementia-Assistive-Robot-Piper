# Running the smart-home node in Wokwi (no second ESP32 needed)

Wokwi simulates an ESP32 in the browser, including its Wi-Fi stack. The simulated
board can open real TCP connections to the **public** internet, so it can talk to a
public MQTT broker. Combined with the virtual house and the backend, this means the
whole smart-home feature can be built and demoed before the second ESP32 arrives.

## One important limitation

On the free tier, a Wokwi simulation **cannot reach your PC's Mosquitto** — the free
public gateway only allows outgoing connections to the internet. Reaching `localhost`
requires the Private IoT Gateway, which is a paid Wokwi feature.

So there are two working configurations:

| Setup | Broker | Who can join |
|---|---|---|
| **A — local** (use this for the real build) | Mosquitto on your PC, `192.168.x.x:1883` | real ESP32, `virtual_house.py`, backend, dashboard |
| **B — Wokwi** (use this while you have no hardware) | `broker.emqx.io:1883` (public) | Wokwi sim, backend, `virtual_house.py`, dashboard |

Both are the same code. You switch with one `#define` in the sketch and one
environment variable in the backend.

## Steps

1. Go to <https://wokwi.com/projects/new/esp32>.
2. Click the **diagram.json** tab, delete everything, paste in this folder's `diagram.json`.
3. Click **sketch.ino**, paste in `firmware/piper_house_node/piper_house_node.ino`.
4. In the sketch set:

   ```cpp
   #define BUILD_FOR_WOKWI 1
   ```

5. **Change `TOPIC_ROOT` to something unique**, e.g. `piper-g2-24013567`. `broker.emqx.io`
   is a public broker — anyone in the world can publish to `piper/cmd/stove`. A unique
   root is your only isolation. Never demo a real relay on a public broker.
6. Click the library tab (📚) and add **PubSubClient** and **ArduinoJson**.
7. Press ▶. The serial monitor should show `connected` and then four `[STATE]` lines.
8. On your PC, point the backend at the same broker and run it:

   ```
   set PIPER_MQTT_HOST=broker.emqx.io
   set PIPER_TOPIC_ROOT=piper-g2-24013567
   python main.py
   ```

   Type `turn on the living room light` — the yellow LED in the browser lights up,
   and the backend prints the state message that came back from the simulated ESP32.

## What Wokwi will and will not prove

Good enough for: MQTT logic, JSON parsing, reconnect behaviour, topic design,
the safety interlock, timing, the whole software contract.

Not simulated: real relay switching, mains isolation, PIR sensor behaviour,
Wi-Fi range, power draw. Those still need the bench test once the parts arrive —
say exactly this in the report's testing section and it reads as good engineering
rather than as a shortcut.

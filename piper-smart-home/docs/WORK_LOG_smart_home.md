# Smart Home (MQTT) — Work Log

**Project:** PIPER, Dementia Assistive Robot — IDP1 Group 2
**Part of the system:** Smart Home Automation (Section 3.2 of the report)
**Done by:** Oh Yu Qiao
**Date:** 8 September 2026

---

## 1. What this part of the robot does

PIPER is meant to control things in the house. If the person says *"turn on the
living room light"*, a light should switch on. If they leave the stove on and walk
away, PIPER should notice and switch it off.

My job was to build the part that carries those commands from the robot's brain to
the appliances in the house.

---

## 2. The problem I had to work around

Our design uses **two ESP32 boards**: one inside the robot, and one in the house
that switches the appliances. We only have **one board** right now, and we do not
have the relays, the microphone, or the mmWave sensor yet.

So instead of waiting for parts, I built the system so that every piece can be
swapped between a **simulated** version and a **real** version without changing
anything else. That way the work could start now and the real hardware just drops
in later.

---

## 3. What I built

| Piece | What it does |
|---|---|
| **MQTT broker** (Mosquitto) | The post office in the middle. Everything sends and receives messages through it. |
| **House node** (ESP32 program) | Listens for commands and switches the lights, fan and stove. Written but not yet flashed to the board. |
| **Virtual house** (Python) | A software copy of that ESP32. Behaves exactly the same, so I can test with no hardware. |
| **Backend** (Python) | The robot's brain. Turns what the person said into a command and sends it. |
| **Safety checker** | Sits between the AI and the appliances. Blocks anything unsafe or invalid. |
| **Rules engine** | The automatic behaviours — night lighting, stove warnings, lights left on in empty rooms. |
| **Diary** (SQLite) | Records what happened, so PIPER can answer *"did I take my medicine?"* later. |
| **Dashboard** (web page) | A screen showing the house live. For the demo video. |

---

## 4. How a command travels

```
Person speaks
   ↓
Speech turned into text
   ↓
Gemini decides what they meant, and replies in a fixed format
   ↓
Backend checks the request is allowed          ← safety happens here
   ↓
Command sent through the MQTT broker
   ↓
ESP32 in the house switches the light
   ↓
ESP32 sends back "the light is now on"
   ↓
Only then does PIPER say "the light is on"
```

The last two lines matter. PIPER does not say a light is on just because it sent
the command. It waits for the light itself to confirm. If nothing comes back, it
tells the person honestly that it could not do it.

---

## 5. Safety decisions

These were deliberate, and they are the parts I would explain in a viva.

**The AI is never trusted.** Gemini is a language model, and language models make
things up. So everything it asks for is checked first: the device must be one we
actually have, the action must be one that device allows, and no more than three
commands can come from one sentence. Anything else is thrown away and written to a
log.

**PIPER can never switch the stove ON.** It can only switch it off. This is
blocked in two separate places — in the backend, and again in the ESP32 itself. If
one has a bug, the other still stops it. A robot for someone with dementia should
never be able to turn on a heating element.

**If the house node dies, PIPER admits it.** The ESP32 tells the broker in advance:
*"if I disappear, tell everyone I am offline."* So when it loses power, PIPER says
*"I can't reach the lights at the moment"* instead of confidently lying to someone
who cannot check for themselves.

---

## 6. Testing

I wrote 19 automated tests. All 19 pass.

They check:

- A command is sent, acted on, and confirmed back
- The system knows the state of every appliance after a restart
- Both stove blocks work
- A made-up device name from the AI is thrown away, not sent
- The night lighting rule turns the light on when someone walks about at night
- The stove rule gives a spoken warning, then cuts the power, then alerts the
  caregiver

The tests run in about 19 seconds and start their own simulated house, so anyone on
the team can run them.

---

## 7. Problems I hit, and how I solved them

**The tests crashed on Windows.** The test file used a Linux folder path
(`/tmp`) that does not exist on Windows. Fixed by asking Python for the correct
temporary folder for whichever computer it is running on. Now it works on all our
machines.

**Everything said "connected" but nothing worked.** The dashboard connected to the
broker, the broker accepted it, and no messages ever arrived. It turned out there
were **two brokers running at once** — one I started by hand, and one Windows had
started automatically. Different programs were connecting to different brokers, so
they never saw each other's messages. Nothing reported an error because, from each
program's point of view, nothing was wrong.

I found it by comparing the process IDs on each port. Fix: stop the automatic one
so only a single broker runs.

This was the most useful thing I learned today. Every part of a system can report
that it is healthy while the system as a whole does nothing. That is exactly why
the design waits for the appliance to confirm, instead of assuming.

**Wokwi would not run.** Wokwi is an online ESP32 simulator. It kept returning
"server busy" — their free build servers were full. This was only ever a way to
test the ESP32 program without a board, and we have a board, so I skipped it and
will test on the real ESP32 instead.

---

## 8. Where the work stands

**Working now:**

- Broker installed and configured
- Full command path, tested end to end
- Both safety blocks
- All three automatic rules
- The diary
- 19 passing tests

**Written but not yet run on hardware:**

- The ESP32 program. It is finished; it needs the board, four LEDs and resistors.

**Not started (other people's parts, or later):**

- Whisper speech-to-text — replaces typing on the keyboard
- Piper text-to-speech — replaces printing on screen
- Real Gemini — one setting switches it on
- Telegram alerts — the same function the fall detection needs
- Broker password. Right now the broker accepts anyone on the network. This must
  be turned on before the demo, because our report says the system keeps data
  private.

---

## 9. What I do next

1. Flash the ESP32 and wire four LEDs, so the same commands switch real lights
2. Turn on the broker password
3. Measure how long a command takes from start to finish, for the report
4. Order the second ESP32 before the 9 October hardware deadline

---

## 10. Note for the report

Be clear about which results came from simulation and which came from hardware.

Simulation proved the message design, the safety blocks, the rules and the timing.
It cannot prove relay switching, mains safety, sensor accuracy, WiFi range or power
use. Those still need bench tests. Saying so plainly is better than blurring it.

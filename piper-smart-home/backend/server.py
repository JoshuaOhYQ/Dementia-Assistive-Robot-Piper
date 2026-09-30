"""PIPER backend — HTTP server for the robot ESP32.

This is the link the report describes in Section 3.4: the robot ESP32 talks to
the Python backend over Wi-Fi using HTTP (Flask), and the backend talks to the
smart-home ESP32 over MQTT.

    robot ESP32 --HTTP POST--> server.py --MQTT--> house ESP32
                <--reply-----            <--state--

Today the robot sends TEXT (typed in its Serial Monitor, or its BOOT button).
When Whisper is integrated, the same endpoint receives the transcript instead.

Every request is written to test_log.csv — open it in Excel for the report's
accuracy and latency figures.

Run:
    pip install flask paho-mqtt
    python server.py
"""
from __future__ import annotations

import csv
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

import config
import pipeline
from diary import Diary
from rules import RuleEngine
from smart_home import SmartHome

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)-18s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)   # quieter request log
log = logging.getLogger("piper.server")

HTTP_PORT = int(os.getenv("PIPER_HTTP_PORT", "5000"))
# PIPER_RULES=off during accuracy runs, so an automatic rule never changes a
# device between a command and the check of that command.
RULES_ON = os.getenv("PIPER_RULES", "on").lower() != "off"
LOG_PATH = Path(os.getenv("PIPER_TEST_LOG", Path(__file__).parent / "test_log.csv"))

LOG_FIELDS = ["id", "time", "source", "text", "llm_mode", "accepted", "rejected",
              "all_ok", "speech", "llm_ms", "mqtt_ms", "backend_ms", "device_rtt_ms",
              "correct_y_n"]

app = Flask(__name__)

diary = Diary()
home = SmartHome()

# One utterance at a time — the report's single-loop design. A second request
# waits for the first to finish instead of racing it.
_pipeline_lock = threading.Lock()
_log_lock = threading.Lock()
_rows: list[dict] = []


# ---------------------------------------------------------------------------
# Test log
# ---------------------------------------------------------------------------

def _load_log():
    if LOG_PATH.exists():
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            _rows.extend(csv.DictReader(f))


def _write_log():
    tmp = LOG_PATH.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(_rows)
    os.replace(tmp, LOG_PATH)    # never leave a half-written CSV if Excel has it open


def _speak(text: str):
    log.info("SPEAK (rule): %s", text)


def _alert(text: str):
    log.warning("TELEGRAM (stub): %s", text)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return ("PIPER backend is running.\n"
            "POST /api/utterance  {\"text\": \"turn on the living room light\"}\n"
            "GET  /api/health\n"
            "GET  /api/state\n"), 200, {"Content-Type": "text/plain"}


@app.get("/api/health")
def health():
    return jsonify({
        "broker_connected": home.client.is_connected(),
        "house_online": home.house_online,
        "llm": pipeline.llm_mode(),
        "rules": "on" if RULES_ON else "off",
        "server_time": int(time.time()),
    })


@app.get("/api/state")
def state():
    return jsonify(home.snapshot())


@app.post("/api/utterance")
def utterance():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    source = str(body.get("source", "http"))[:20]
    if not text:
        return jsonify({"error": "send JSON like {\"text\": \"turn on the fan\"}"}), 400
    if len(text) > 300:
        return jsonify({"error": "text longer than 300 characters"}), 400

    req_id = uuid.uuid4().hex[:8]
    with _pipeline_lock:
        result = pipeline.process(text, home, diary, source="voice")

    t = result["timing_ms"]
    log.info("[%s] %s: %r -> %r  (llm %.0f ms, mqtt %.0f ms)",
             req_id, source, text, result["speech"], t["llm"], t["mqtt_roundtrip"])

    row = {
        "id": req_id,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "text": text,
        "llm_mode": result["llm_mode"],
        "accepted": "; ".join(
            f'{c["device"]} {c["action"]}' + (f' {c["value"]}' if "value" in c else "")
            for c in result["accepted"]),
        "rejected": "; ".join(result["rejected"]),
        "all_ok": "yes" if result["all_ok"] else "NO",
        "speech": result["speech"],
        "llm_ms": t["llm"],
        "mqtt_ms": t["mqtt_roundtrip"],
        "backend_ms": t["backend_total"],
        "device_rtt_ms": "",
        "correct_y_n": "",
    }
    with _log_lock:
        _rows.append(row)
        _write_log()

    return jsonify({"id": req_id, **result})


@app.post("/api/rtt")
def rtt():
    """The robot ESP32 reports the round trip it measured, so the log has the
    true end-to-end figure (Wi-Fi + HTTP + LLM + MQTT + house ESP32), not just
    the backend's share of it."""
    body = request.get_json(silent=True) or {}
    rid, ms = str(body.get("id", "")), body.get("rtt_ms")
    if not rid or not isinstance(ms, (int, float)):
        return jsonify({"error": "send {\"id\": ..., \"rtt_ms\": ...}"}), 400
    with _log_lock:
        for row in reversed(_rows):
            if row.get("id") == rid:
                row["device_rtt_ms"] = ms
                _write_log()
                return jsonify({"ok": True})
    return jsonify({"error": "unknown id"}), 404


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def _lan_ip() -> str:
    """The address the ESP32s should use. No packet is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _monitoring_loop(engine: RuleEngine):
    while True:
        try:
            engine.tick()
        except Exception:                                   # noqa: BLE001
            log.exception("rule engine error")
        time.sleep(5)


def main():
    _load_log()
    if not home.connect():
        log.error("broker not reachable at %s:%s — is Mosquitto running?",
                  config.MQTT_HOST, config.MQTT_PORT)
    time.sleep(1.5)                                         # let retained state arrive

    if RULES_ON:
        engine = RuleEngine(home, diary, speak=_speak, alert=_alert)
        threading.Thread(target=_monitoring_loop, args=(engine,), daemon=True).start()

    ip = _lan_ip()
    print(f"""
  PIPER backend server
  --------------------
  broker      : {config.MQTT_HOST}:{config.MQTT_PORT}   ({'connected' if home.client.is_connected() else 'NOT CONNECTED'})
  house01     : {'ONLINE' if home.house_online else 'offline'}
  llm         : {pipeline.llm_mode()}
  rules       : {'on' if RULES_ON else 'OFF (accuracy mode)'}
  test log    : {LOG_PATH}

  Put this in piper_robot_node.ino:
      BACKEND_HOST = "{ip}"
      BACKEND_PORT = {HTTP_PORT}

  Quick check from this PC:   http://{ip}:{HTTP_PORT}/api/health
""")
    # threaded=True lets /api/health answer while an utterance is being processed;
    # the pipeline lock above still keeps utterances strictly one at a time.
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

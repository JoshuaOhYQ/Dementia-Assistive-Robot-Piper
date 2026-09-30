"""PIPER preflight — run this before every test session.

Checks the things that cost us an hour last time, in about five seconds:

  * exactly ONE broker is running (the two-broker split that made everything
    report "connected" while no message ever arrived)
  * a message published one way arrives on every other path — localhost,
    127.0.0.1, IPv6 ::1, your LAN IP (the one the ESP32s use), and WebSockets
    (the one the dashboard uses)
  * whether the house ESP32 is online
  * whether the backend HTTP server is up
  * whether the Windows Firewall rules exist
  * your PC's LAN IP, to paste into both .ino files

    python preflight.py
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import paho.mqtt.client as mqtt

ROOT = os.getenv("PIPER_TOPIC_ROOT", "piper")
IS_WIN = platform.system() == "Windows"

GREEN, RED, YELLOW, DIM, END = ("\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m")
if IS_WIN:
    os.system("")                       # enables ANSI colours in Windows terminals

problems: list[str] = []


def ok(msg):   print(f"  {GREEN}PASS{END}  {msg}")
def bad(msg, fix):
    print(f"  {RED}FAIL{END}  {msg}\n        {DIM}fix:{END} {fix}")
    problems.append(msg)
def warn(msg): print(f"  {YELLOW}WARN{END}  {msg}")
def info(msg): print(f"  {DIM}....{END}  {msg}")


def make_client(cid, transport="tcp"):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid, transport=transport)
    except AttributeError:
        return mqtt.Client(client_id=cid, transport=transport)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))      # no packet is sent; this just picks the route
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def port_open(host, port, family=socket.AF_INET) -> bool:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect((host, port))
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 1. Who is listening?
# ---------------------------------------------------------------------------

def listening_pids(port: int) -> dict[str, str]:
    """address -> PID, for sockets LISTENING on the port (Windows only)."""
    if not IS_WIN:
        return {}
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[1].endswith(f":{port}"):
                found[parts[1]] = parts[4]
    return found


def process_name(pid: str) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout.strip()
        return out.split(",")[0].strip('"') if out else "?"
    except Exception:                                            # noqa: BLE001
        return "?"


def check_single_broker():
    print("\n  Broker processes")
    if not IS_WIN:
        info("process check only runs on Windows — skipped")
        return
    l1883, l9001 = listening_pids(1883), listening_pids(9001)
    if not l1883:
        bad("nothing is listening on port 1883",
            'start the broker:  & "C:\\Program Files\\mosquitto\\mosquitto.exe" '
            '-c "C:\\Program Files\\mosquitto\\mosquitto.conf" -v')
        return
    pids = set(l1883.values()) | set(l9001.values())
    for addr, pid in {**l1883, **l9001}.items():
        info(f"{addr:22s} PID {pid}  ({process_name(pid)})")
    if len(pids) > 1:
        bad(f"{len(pids)} different broker processes are running (PIDs {', '.join(sorted(pids))})",
            "Administrator PowerShell:  Stop-Service mosquitto; "
            "Set-Service mosquitto -StartupType Manual   — then restart your broker window")
    else:
        ok(f"one broker process owns every MQTT port (PID {pids.pop()})")
    if not l9001:
        warn("nothing on 9001 — the browser dashboard will not connect (everything else is fine)")


# ---------------------------------------------------------------------------
# 2. Does a message published one way arrive on every path?
# ---------------------------------------------------------------------------

def check_same_broker(ip: str):
    print("\n  Message delivery across every path")
    paths = [
        ("localhost:1883  (backend, virtual house)", "localhost", 1883, "tcp"),
        ("127.0.0.1:1883",                           "127.0.0.1", 1883, "tcp"),
        ("[::1]:1883      (IPv6 localhost)",         "::1",       1883, "tcp"),
    ]
    if ip:
        paths.append((f"{ip}:1883  (what the ESP32s use)", ip, 1883, "tcp"))
    paths.append(("ws 127.0.0.1:9001 (dashboard)", "127.0.0.1", 9001, "websockets"))

    nonce = uuid.uuid4().hex[:10]
    topic = f"piper-preflight/{nonce}"
    received: dict[str, threading.Event] = {}
    clients = []

    for label, host, port, transport in paths:
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        if host != "localhost" and not port_open(host, port, fam):
            if "ESP32" in label:
                bad(f"{label} — nothing listening on the LAN address",
                    "mosquitto.conf needs  listener 1883 0.0.0.0  (the default config "
                    "listens on localhost only, so the ESP32s can never connect)")
            else:
                info(f"{label:44s} not listening — skipped")
            continue
        ev = threading.Event()
        c = make_client(f"preflight-{uuid.uuid4().hex[:6]}", transport)
        c.on_message = (lambda e: lambda *_: e.set())(ev)
        c.on_connect = (lambda t: lambda cl, *_: cl.subscribe(t, qos=1))(topic)
        try:
            c.connect(host, port, keepalive=10)
            c.loop_start()
            received[label] = ev
            clients.append(c)
        except Exception as e:                                   # noqa: BLE001
            bad(f"{label} — cannot connect ({e})", "is the broker running?")

    if not clients:
        return
    time.sleep(1.2)                                              # let subscriptions land

    pub = make_client("preflight-pub")
    pub.connect("localhost", 1883, keepalive=10)
    pub.loop_start()
    pub.publish(topic, json.dumps({"preflight": nonce}), qos=1).wait_for_publish(3)
    time.sleep(1.5)
    pub.loop_stop(); pub.disconnect()

    missing = []
    for label, ev in received.items():
        if ev.is_set():
            ok(f"{label}")
        else:
            missing.append(label)
            print(f"  {RED}FAIL{END}  {label}  — never received the test message")
    for c in clients:
        c.loop_stop(); c.disconnect()

    if missing:
        problems.append("split brokers")
        print(f"        {DIM}fix:{END} these paths reach a DIFFERENT broker from 'localhost'. "
              "Almost always the Windows service — see the process list above.")


# ---------------------------------------------------------------------------
# 3. Nodes and servers
# ---------------------------------------------------------------------------

def check_house():
    print("\n  Smart-home node")
    got = {}
    c = make_client("preflight-house")
    c.on_connect = lambda cl, *_: cl.subscribe(f"{ROOT}/availability/house01", qos=1)
    c.on_message = lambda cl, u, m: got.setdefault("v", m.payload.decode(errors="ignore"))
    try:
        c.connect("localhost", 1883, keepalive=10)
    except Exception:                                            # noqa: BLE001
        bad("cannot reach broker on localhost:1883", "start the broker first")
        return
    c.loop_start(); time.sleep(1.5); c.loop_stop(); c.disconnect()
    v = got.get("v")
    if v == "online":
        ok("house01 is online")
    elif v == "offline":
        warn("house01 is OFFLINE — power the house ESP32 (or start virtual_house.py)")
    else:
        warn("house01 has never connected on this topic root — not started yet")


def check_server(ip: str):
    print("\n  Backend HTTP server (robot ESP32 link)")
    for host in filter(None, ["127.0.0.1", ip]):
        try:
            with urllib.request.urlopen(f"http://{host}:5000/api/health", timeout=2) as r:
                h = json.loads(r.read())
            ok(f"http://{host}:5000 answers — llm: {h.get('llm')}, "
               f"house_online: {h.get('house_online')}")
        except Exception:                                        # noqa: BLE001
            warn(f"http://{host}:5000 not answering — start  python server.py  "
                 "(only needed for the robot ESP32)")
            return


def check_firewall():
    print("\n  Windows Firewall (ESP32s connect from outside this PC)")
    if not IS_WIN:
        info("Windows only — skipped")
        return
    for name, port in (("Mosquitto MQTT 1883", 1883), ("PIPER backend 5000", 5000)):
        out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                             capture_output=True, text=True).stdout
        if "No rules match" in out or not out.strip():
            bad(f"no inbound rule '{name}'",
                f"Administrator PowerShell:  New-NetFirewallRule -DisplayName \"{name}\" "
                f"-Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow -Profile Private")
        else:
            ok(f"rule '{name}' exists")

    # The rules above are for Private networks. Windows labels most new networks —
    # including a phone hotspot — as Public, and then the rules silently do nothing.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetConnectionProfile | Select-Object Name,NetworkCategory | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10).stdout
        profiles = json.loads(out) if out.strip() else []
        if isinstance(profiles, dict):
            profiles = [profiles]
        for p in profiles:
            cat = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}.get(
                p.get("NetworkCategory"), str(p.get("NetworkCategory")))
            if cat == "Public":
                bad(f"network '{p.get('Name')}' is marked Public — the firewall rules above "
                    "do not apply to it",
                    "Settings → Network & internet → Wi-Fi → click the network → "
                    "Network profile type → Private")
            else:
                ok(f"network '{p.get('Name')}' is {cat}")
    except Exception:                                            # noqa: BLE001
        info("could not read the network profile — check Settings → Wi-Fi → Private manually")
    info("a rule existing is not proof it works — the real test is from ANOTHER device:")
    info("open http://<LAN IP>:5000/api/health in your phone's browser on the same Wi-Fi")


# ---------------------------------------------------------------------------

def main():
    print("\n  PIPER preflight")
    print("  ===============")
    ip = lan_ip()
    if ip:
        print(f"\n  Your PC's LAN IP is  {GREEN}{ip}{END}")
        print(f"  → house node:  MQTT_HOST    = \"{ip}\"")
        print(f"  → robot node:  BACKEND_HOST = \"{ip}\"")
        if ip.startswith("169.254."):
            warn("that is a self-assigned address — the PC is not really on a network")
    else:
        warn("could not work out a LAN IP — is Wi-Fi connected?")

    check_single_broker()
    check_same_broker(ip)
    check_house()
    check_server(ip)
    check_firewall()

    print()
    if problems:
        print(f"  {RED}{len(problems)} problem(s) — fix the FAIL lines above before testing.{END}\n")
        sys.exit(1)
    print(f"  {GREEN}All clear.{END}\n")


if __name__ == "__main__":
    main()

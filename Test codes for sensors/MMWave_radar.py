import serial, struct
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

PORT = "COM4"

def dec(v):                      # sensor's signed format: top bit set = positive
    return (v & 0x7FFF) if v & 0x8000 else -(v & 0x7FFF)

s = serial.Serial(PORT, 256000, timeout=0)
buf = b""

fig, ax = plt.subplots(figsize=(8, 6))
ax.set_xlim(-5500, 5500); ax.set_ylim(0, 6500); ax.set_aspect("equal")
ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm), distance ahead of sensor")
ax.set_title("LD2450 live view (sensor at the bottom centre)")

# field of view: about 60 degrees either side, out to 6 m
ang = np.radians(np.linspace(30, 150, 100))
for r in (2000, 4000, 6000):
    ax.plot(r * np.cos(ang), r * np.sin(ang), color="grey", lw=0.8)
for a in (ang[0], ang[-1]):
    ax.plot([0, 6000 * np.cos(a)], [0, 6000 * np.sin(a)], color="grey", lw=0.8)

dots = ax.scatter([], [], s=200)
labels = [ax.text(0, 0, "", fontsize=9, visible=False) for _ in range(3)]

def update(_):
    global buf
    buf += s.read(4096)
    frame = None
    while True:                  # skip to the newest complete packet
        i = buf.find(b"\xAA\xFF\x03\x00")
        if i < 0 or len(buf) < i + 30:
            break
        frame, buf = buf[i:i+30], buf[i+30:]
    if frame is None:
        return
    pts = []
    for t in range(3):
        x, y, spd, _r = struct.unpack_from("<HHHH", frame, 4 + t*8)
        if x or y:
            x, y = dec(x), dec(y)
            pts.append((x, y))
            labels[t].set_position((x + 150, y + 150))
            labels[t].set_text(f"T{t+1}  {y/1000:.1f} m")
            labels[t].set_visible(True)
        else:
            labels[t].set_visible(False)
    dots.set_offsets(pts if pts else np.empty((0, 2)))

ani = FuncAnimation(fig, update, interval=50, cache_frame_data=False)
plt.show()
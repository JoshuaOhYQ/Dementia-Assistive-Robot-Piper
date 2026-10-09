import serial, struct, time

INTERVAL = 0.5                   # seconds between printouts

def dec(v):                      # sensor's signed format: top bit set = positive
    return (v & 0x7FFF) if v & 0x8000 else -(v & 0x7FFF)

s = serial.Serial("COM4", 256000, timeout=1)
buf = b""
latest = None
last_print = 0
while True:
    buf += s.read(s.in_waiting or 1)
    while True:
        i = buf.find(b"\xAA\xFF\x03\x00")
        if i < 0 or len(buf) < i + 30:
            break
        latest, buf = buf[i:i+30], buf[i+30:]

    if latest and time.time() - last_print >= INTERVAL:
        last_print = time.time()
        seen = False
        for t in range(3):
            x, y, spd, _ = struct.unpack_from("<HHHH", latest, 4 + t*8)
            if x or y:
                seen = True
                print(f"target {t+1}: x={dec(x)} mm  y={dec(y)} mm  speed={dec(spd)} cm/s")
        print("---" if seen else "no target")
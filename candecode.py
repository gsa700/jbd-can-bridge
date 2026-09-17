#!/usr/bin/env python3
"""Decode Pylontech-profile frames back to human values.

Reads `candump -L <iface>` on stdin. Use it to confirm what the GX is actually
being told -- on vcan0 while developing, and on the real bus at commissioning.

    candump -L vcan0 | ./candecode.py
"""
import sys, struct

def s16(b, i): return struct.unpack_from("<h", b, i)[0]
def u16(b, i): return struct.unpack_from("<H", b, i)[0]

BITS0 = [(1 << 1, "CELL_OV"), (1 << 2, "CELL_UV"), (1 << 3, "OVER_TEMP"),
         (1 << 4, "UNDER_TEMP"), (1 << 7, "DISCHARGE_OC")]
BITS1 = [(1 << 0, "CHARGE_OC"), (1 << 3, "SYSTEM_ERROR")]

def flags(b0, b1):
    f = [n for m, n in BITS0 if b0 & m] + [n for m, n in BITS1 if b1 & m]
    return ",".join(f) if f else "none"

def decode(cid, d):
    if cid == 0x351 and len(d) >= 8:
        return (f"CVL {u16(d,0)/10:.1f} V   CCL {s16(d,2)/10:.1f} A   "
                f"DCL {s16(d,4)/10:.1f} A   DVL {u16(d,6)/10:.1f} V")
    if cid == 0x355 and len(d) >= 4:
        return f"SOC {u16(d,0)} %   SOH {u16(d,2)} %"
    if cid == 0x356 and len(d) >= 6:
        return (f"{s16(d,0)/100:.2f} V   {s16(d,2)/10:+.1f} A   "
                f"{s16(d,4)/10:.1f} C")
    if cid == 0x359 and len(d) >= 7:
        return (f"errors [{flags(d[0], d[1])}]   warnings [{flags(d[2], d[3])}]   "
                f"packs {d[4]}   id {chr(d[5])}{chr(d[6])}")
    if cid == 0x35C and len(d) >= 1:
        c = "charge" if d[0] & 0x80 else "-"
        g = "discharge" if d[0] & 0x40 else "-"
        return f"enabled: {c} {g}"
    if cid == 0x35E:
        return "manufacturer " + repr(d.decode("ascii", "replace"))
    return d.hex(" ")

NAMES = {0x351: "limits", 0x355: "soc/soh", 0x356: "measured",
         0x359: "alarms", 0x35C: "enable", 0x35E: "name"}

for line in sys.stdin:
    parts = line.split()
    if len(parts) < 2 or "#" not in parts[-1]:
        continue
    cid_s, _, data_s = parts[-1].partition("#")
    try:
        cid = int(cid_s, 16); data = bytes.fromhex(data_s)
    except ValueError:
        continue
    print(f"0x{cid:03X} {NAMES.get(cid,'?'):9s} {decode(cid, data)}")

#!/usr/bin/env python3
"""Probe which JBD registers answer a READ. STRICTLY READ ONLY.

Emits ONLY 0xA5 (read) frames. There is no 0x5A anywhere in this file, so it
cannot write a register and cannot enter factory/EEPROM mode (which itself
requires a write). The OTA/DFU service is never addressed.
"""
import asyncio, sys
from bleak import BleakScanner, BleakClient
CH_TX = "0000ff02-0000-1000-8000-00805f9b34fb"
CH_RX = "0000ff01-0000-1000-8000-00805f9b34fb"
CMD_READ = 0xA5

def read_frame(reg):
    chk = (0x10000 - reg) & 0xFFFF
    return bytes([0xDD, CMD_READ, reg, 0x00, chk >> 8, chk & 0xFF, 0x77])

KNOWN = {0x03: "basic info", 0x04: "cell voltages", 0x05: "version string"}

async def main():
    addr = sys.argv[1]
    lo, hi = int(sys.argv[2], 0), int(sys.argv[3], 0)
    dev = await BleakScanner.find_device_by_address(addr, timeout=15.0)
    if not dev: print("device not found"); return
    buf = bytearray()
    def on_notify(_, d): buf.extend(d)
    hits, errs, silent = [], 0, 0
    async with BleakClient(dev) as cl:
        await cl.start_notify(CH_RX, on_notify)
        print(f"probing 0x{lo:02x}-0x{hi:02x} with read-only 0xA5 frames\n")
        for reg in range(lo, hi + 1):
            if not cl.is_connected:
                print("!! BMS dropped the connection — stopping"); break
            buf.clear()
            try:
                await cl.write_gatt_char(CH_TX, read_frame(reg), response=False)
            except Exception as e:
                print(f"  0x{reg:02x}: write failed ({e}) — stopping"); break
            await asyncio.sleep(0.55)
            if not buf: silent += 1; continue
            f = bytes(buf)
            if len(f) < 7 or f[0] != 0xDD: continue
            status, ln = f[2], f[3]
            if status != 0: errs += 1; continue
            data = f[4:4+ln]
            note = KNOWN.get(reg, "")
            hits.append((reg, data, note))
            print(f"  0x{reg:02x}  len={ln:3d}  {data.hex()}  {note}")
        await cl.stop_notify(CH_RX)
    print(f"\n{len(hits)} registers answered · {errs} returned an error status · {silent} silent")
    unknown = [h for h in hits if h[0] not in KNOWN]
    if unknown:
        print("\nUNDOCUMENTED registers that returned data:")
        for reg, data, _ in unknown:
            print(f"  0x{reg:02x}  {len(data)} bytes  {data.hex()}")
    else:
        print("\nNo undocumented registers answered outside the standard set.")
asyncio.run(main())

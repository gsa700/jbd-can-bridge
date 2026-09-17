#!/usr/bin/env python3
"""JBD BMS reader — STRICTLY READ ONLY.

The only frame builder in this file hard-codes CMD_READ (0xA5). There is no
function anywhere that can emit 0x5A (write-register), so no code path exists
that could change a BMS setting. The OTA/DFU service is never addressed.
"""
import asyncio, sys
from bleak import BleakScanner, BleakClient

SVC    = "0000ff00-0000-1000-8000-00805f9b34fb"
CH_TX  = "0000ff02-0000-1000-8000-00805f9b34fb"   # we write requests here
CH_RX  = "0000ff01-0000-1000-8000-00805f9b34fb"   # answers arrive here
CMD_READ = 0xA5                                    # 0x5A (write) is deliberately absent

def read_frame(reg: int) -> bytes:
    """Build a READ request. reg only; no payload can be attached."""
    chk = (0x10000 - reg) & 0xFFFF
    return bytes([0xDD, CMD_READ, reg, 0x00, chk >> 8, chk & 0xFF, 0x77])

def split_frames(buf: bytearray):
    out = []
    while len(buf) >= 7 and buf[0] == 0xDD:
        ln = buf[3]; total = ln + 7
        if len(buf) < total: break
        out.append(bytes(buf[:total])); del buf[:total]
    return out

def u16(b, i): return (b[i] << 8) | b[i+1]
def s16(b, i):
    v = u16(b, i); return v - 0x10000 if v & 0x8000 else v

PROT = ["cell overvolt","cell undervolt","pack overvolt","pack undervolt",
        "charge over-temp","charge under-temp","discharge over-temp","discharge under-temp",
        "charge overcurrent","discharge overcurrent","short circuit","front-end IC error",
        "MOS software lock"]

async def main():
    addr = sys.argv[1]
    dev = await BleakScanner.find_device_by_address(addr, timeout=15.0)
    if not dev: print("device not found"); return
    buf = bytearray(); frames = {}
    def on_notify(_, data): buf.extend(data)

    async with BleakClient(dev) as cl:
        print(f"connected {addr}\n")
        await cl.start_notify(CH_RX, on_notify)
        for reg in (0x03, 0x04, 0x05):
            buf.clear()
            await cl.write_gatt_char(CH_TX, read_frame(reg), response=False)
            await asyncio.sleep(1.2)
            for f in split_frames(buf):
                frames[f[1]] = f
        await cl.stop_notify(CH_RX)

    f = frames.get(0x03)
    if f and f[2] == 0:
        d = f[4:4+f[3]]
        print("=== 0x03 basic info ===")
        print(f"  pack voltage      {u16(d,0)/100:.2f} V")
        print(f"  current           {s16(d,2)/100:+.2f} A   ({'charging' if s16(d,2)>0 else 'discharging' if s16(d,2)<0 else 'idle'})")
        print(f"  residual capacity {u16(d,4)/100:.2f} Ah")
        print(f"  nominal capacity  {u16(d,6)/100:.2f} Ah")
        print(f"  cycles            {u16(d,8)}")
        pd = u16(d,10); print(f"  production date   {2000+(pd>>9)}-{(pd>>5)&0x0F:02d}-{pd&0x1F:02d}")
        print(f"  SOC (BMS)         {d[19]} %")
        fet = d[20]; print(f"  FETs              charge={'ON' if fet&1 else 'off'}  discharge={'ON' if fet&2 else 'off'}")
        print(f"  cells             {d[21]}")
        bal = u16(d,12) | (u16(d,14) << 16)
        print(f"  balancing         {'none' if bal==0 else hex(bal)}")
        prot = u16(d,16)
        print(f"  protection        {'NONE (healthy)' if prot==0 else ', '.join(n for i,n in enumerate(PROT) if prot>>i & 1)}")
        ntc = d[22]
        for i in range(ntc):
            t = (u16(d, 23+i*2) - 2731) / 10
            print(f"  temp sensor {i+1}     {t:.1f} C  ({t*9/5+32:.1f} F)")
    else:
        print("no valid 0x03 response")

    f5 = frames.get(0x05)
    if f5 and f5[2] == 0:
        d5 = f5[4:4+f5[3]]
        print(f"\n=== 0x05 version string ===\n  {d5.decode('ascii', 'replace')!r}")
    print("\n=== raw frames (known-good reference for any future decoder) ===")
    for reg in sorted(frames):
        print(f"  0x{reg:02x}: {frames[reg].hex()}")

    f = frames.get(0x04)
    if f and f[2] == 0:
        d = f[4:4+f[3]]
        mv = [u16(d,i*2) for i in range(len(d)//2)]
        print("\n=== 0x04 cell voltages ===")
        for i, v in enumerate(mv): print(f"  cell {i+1}  {v/1000:.3f} V")
        if mv:
            print(f"  spread   {max(mv)-min(mv)} mV   (min {min(mv)/1000:.3f}  max {max(mv)/1000:.3f})")
asyncio.run(main())


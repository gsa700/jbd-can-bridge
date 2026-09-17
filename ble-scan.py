#!/usr/bin/env python3
"""Passive BLE discovery. READ ONLY — this only listens to advertisements and,
optionally, enumerates GATT services. It never writes a characteristic."""
import asyncio, sys
from bleak import BleakScanner, BleakClient

async def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    print(f"scanning {secs:.0f}s ...\n")
    found = await BleakScanner.discover(timeout=secs, return_adv=True)
    if not found:
        print("nothing seen — is Bluetooth on?"); return
    rows = sorted(found.values(), key=lambda t: -(t[1].rssi or -999))
    jbd = []
    for dev, adv in rows:
        name = adv.local_name or dev.name or "(no name)"
        # Match on the SERVICE UUID, not the name. A JBD BMS does not advertise
        # "JBD" anywhere -- the Epoch advertises only a date code (202607130144
        # = build date + unit number). The apps add the JBD label themselves.
        # Filtering on the name finds nothing and reports a false negative.
        tag = ""
        if any(u.lower().startswith("0000ff00") for u in (adv.service_uuids or [])):
            tag = "   <<< JBD BMS (service 0xff00)"; jbd.append((dev, adv, name))
        print(f"  {dev.address}  {adv.rssi:4d} dBm  {name}{tag}")
        if adv.service_uuids:
            print(f"      services: {', '.join(adv.service_uuids)}")
        if adv.manufacturer_data:
            for k, v in adv.manufacturer_data.items():
                print(f"      mfr 0x{k:04x}: {v.hex()}")
    if not jbd:
        print("\nNo device advertising service 0xff00. If the phone app is CONNECTED to the")
        print("battery, it may stop advertising or refuse a second connection — close it and retry.")
        return
    for dev, adv, name in jbd:
        print(f"\n=== GATT enumeration for {name} ({dev.address}) — read only ===")
        try:
            async with BleakClient(dev) as cl:
                for svc in cl.services:
                    print(f"  service {svc.uuid}  {svc.description}")
                    for ch in svc.characteristics:
                        print(f"    char {ch.uuid}  {','.join(ch.properties)}")
        except Exception as e:
            print(f"  connect failed: {e}")
            print("  (single-connection BMS: close the phone app and try again)")

asyncio.run(main())

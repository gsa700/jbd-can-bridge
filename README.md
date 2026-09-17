# jbd-can-bridge — a JBD BMS on any Pylontech-compatible inverter, read-only

Reads a **JBD / Jiabaida** BMS over Bluetooth LE and re-presents it as **Pylontech-profile CAN**
— the published inverter-battery protocol. Your inverter or charge controller then sees an ordinary
managed battery.

**Built and tested for a Victron GX** (Cerbo GX MK2, Venus OS v3.79), where the payoff is specific:
the GX shows a managed battery using **nothing but its own firmware — no driver installed, no root,
no third-party software on the GX at all**, which is the usual price of getting a JBD onto Venus.

**Nothing in the CAN code is Victron-specific**, so it should work with anything that speaks
Pylontech LV CAN — Deye, Sol-Ark, SMA Sunny Island, Growatt, Goodwe, Luxpower and others. Only the
wiring notes in `deploy/` are Victron's (its RJ45 uses pins 7/8/3; most other brands use 4/5, which
is why Victron sells Type A and Type B adapter cables). Untested outside Victron — reports welcome.

The battery end is equally generic: any JBD-based pack should work, including Overkill Solar and
most rebadged 12 V LiFePO4.

> ## ⚠ READ THIS BEFORE YOU RUN IT
>
> **The charge limits in `canframes.py` are for a 12 V, 105 Ah LiFePO4 pack, and the GX will
> obey them.** Presenting a managed CAN battery makes Venus **force DVCC on**, which turns
> `CVL_V`, `CCL_A` and `DCL_A` into live control values — the GX commands your charger with them.
> The defaults are `CVL_V = 14.2` (the bottom of Epoch's 14.2–14.4 V range), `CCL_A = 50`,
> `DCL_A = 80`, `DVL_V = 11.0`.
>
> **On a 24 V or 48 V bank these numbers are dangerously wrong** — a 48 V system told to charge to
> 14.2 V is not a cosmetic error. **Set them for your pack before first connection**, from your
> cell chemistry and the manufacturer's spec, not from this file.
>
> Read-only applies to the **battery**, not to your charger: this code never writes to the BMS,
> but the GX acts on the limits it sends.

The pack this was built against is an Epoch 12105A-H, whose BMS is a **JBD (Jiabaida)**
`DP04S007L4S100A`, reachable only over Bluetooth LE — the battery exposes no serial or CAN port.
That is the usual situation for drop-in 12 V LiFePO4: a JBD inside, Bluetooth out, and nothing a
GX can talk to.

**STRICTLY READ ONLY.** `jbd-read.py` hard-codes the JBD read command `0xA5`. There is no function
in it that can emit `0x5A` (write-register), so no code path exists that could change a BMS setting.
The BMS also exposes an OTA/DFU service (`00010203-0405-...-1912`); it is never addressed.

## Use

    python3 -m venv .venv && .venv/bin/pip install bleak
    .venv/bin/python ble-scan.py 12                       # find it: look for service 0xff00
    .venv/bin/python jbd-read.py AA:BB:CC:DD:EE:FF        # registers 0x03, 0x04, 0x05
    .venv/bin/python jbd-probe.py AA:BB:CC:DD:EE:FF 0x00 0x2f   # which registers answer at all

**Do not look for "JBD" in the advertised name** — it is not there. The battery advertises only a
date-code (`202607130144` = build date + unit number). **Service UUID `0xFF00` is the identifier.**

## Register `0x0f` — use this one

An undocumented register that returns everything `0x03` and `0x04` carry in a SINGLE read, plus
explicit max and min cell voltage. One request per poll instead of two: half the BLE traffic, and an
atomic snapshot rather than two reads a second apart. Offsets (verified against 0x03/0x04):

    1 pack V · 4 SOC · 6 residual · 8 nominal · 12 MAX cell · 14 MIN cell
    20 cycles · 22 FET · 24 temp · 26 cell count · 27+ individual cells

Undocumented, so a firmware revision could move those offsets. **Keep 0x03 + 0x04 as the documented
fallback.**

**Close the phone app first** — the BMS may refuse a second connection while the app holds it.

Keep a commissioning baseline of your own — the raw known-good frames from a healthy pack are
what you validate any future decoder change against.

## Hardware — what you need to build this

| part | notes |
|---|---|
| **Raspberry Pi Zero 2 W** | anything with USB and Bluetooth works; a Zero 2 W is ample for a 1 Hz job |
| **Innomaker USB2CAN** (or any `gs_usb` adapter) | enumerates as `1d50:606f`, driver **`gs_usb`**, already in the Raspberry Pi OS kernel — no driver install. **Get the isolated version.** |
| **micro-USB OTG adapter** | the Zero 2 W's data port is micro-USB and the adapter ends in USB-A. Without this it will not enumerate at all. |
| RJ45 plug + a short length of cable | to make the DB9→RJ45 lead |
| **2 × 120 Ω termination** | see below — both ends, not one |

**Why isolated:** the CAN port on many GX units is *not* galvanically isolated (the original Cerbo GX
is not; the MK2's VE.Can 1 is). An isolated adapter breaks any ground loop between the Pi's supply
and the inverter, which matters more the closer this sits to transmitters or heavy DC.

**The Pi's USB port must be a host.** If it has been used for USB-gadget serial, it is in peripheral
mode and the adapter will not appear. Full instructions in [`deploy/`](deploy/) — briefly: comment
out `dtoverlay=dwc2,dr_mode=peripheral`, stop `g_serial` loading, reboot. You know it worked when a
root hub shows up in `lsusb` and `/sys/class/udc/` is gone.

### Wiring, DB9 to RJ45

    DB9 7  ->  RJ45 7   CAN-H        DB9 2  ->  RJ45 8   CAN-L        DB9 3  ->  RJ45 3   GND

**That RJ45 pinout is Victron's.** Most other brands use pins **4/5** for CAN-H/L — check your
inverter's manual. The DB9 side is standard CiA.

**Terminate both ends.** A two-node CAN bus wants 120 Ω at each end: the jumper on the DB9 breakout,
and a terminator in the spare jack of the same port pair on the GX (Victron ships them with VE.Can
products). Measure across RJ45 pins 7–8 with nothing else connected — open means fit one, ~120 Ω
means that port is already terminated and you should not add another.

### Confirming the link before you trust it

Send a few frames and check the **transmit error count**. CAN cannot transmit successfully unless
another node acknowledges each frame, so `TX packets` climbing with `errors=0` proves the far end is
listening. Swapped CAN-H/CAN-L gives errors and a rising fault count instead. `ERROR-ACTIVE` is the
normal healthy state, not a fault.

    ip -s link show can0        # TX packets rising, errors 0
    ip -d link show can0        # state ERROR-ACTIVE, bitrate 500000

## The architecture, as built

    Epoch BMS --BLE (read-only)--> Pi Zero 2 W --USB--> can0 @500k
              --VE.Can 1--> STOCK Cerbo GX MK2 --Modbus TCP / MQTT--> your app

**Nothing is installed on the Cerbo.** No driver, no root, no third-party software — the battery
appears because Venus OS speaks CAN-bus BMS natively, the same protocol Pylontech and BYD use. The
Cerbo reports its firmware as unmodified and root access is switched off.

This replaced an earlier design that re-served JBD frames over USB-gadget serial to
`dbus-serialbattery`. That worked, but it required rooting the Cerbo, and a hard reset proved it did
not come back on its own: the USB link survived while the battery service stayed absent for over
five minutes and needed a manual `svc -t`. Those notes are kept at the end of this file, because
they explain why the design changed.

Verified: **Cerbo reboot — battery back unassisted 1 s after ssh returned. Pi reboot — `can0` came up
automatically and the battery never dropped.**

## bms-bridge.py — the daemon

Reads the BMS over BLE on its own clock, emits **Pylontech-profile CAN frames** on `can0` at 1 Hz,
and exposes Prometheus metrics on :9101. Installed on the bridge host as `bms-bridge.service`,
running unprivileged — socketcan needs no privilege to send and BlueZ allows the user session.

The verbatim-JBD-frame serial responder is still in the code and still works, but is **off by
default** (`BMS_SERIAL=` empty) and only starts if the device actually exists. See `canframes.py`
for the wire format and `deploy/` for the host configuration.

**Read-only in both directions.** Toward the battery, the only frame builder hard-codes `0xA5` and no
function can emit `0x5A`. Toward the Cerbo, any inbound non-read frame is dropped and counted — the
bridge is a read-only firewall, verified on the wire by sending it a `0x5A` frame and watching it
refuse.

**Cache-and-serve, not a proxy.** A transparent proxy would inherit BLE's latency and dropouts and the
host's serial reads would time out whenever the radio hiccuped. Here a stall becomes stale data.
Cached frames are served while younger than `BMS_MAX_SERVE_AGE` (60 s, ~2 missed polls); past that the
bridge **stops answering** so the host marks the battery absent rather than acting on minutes-old
data — which matters because dbus-serialbattery can feed charge limits into DVCC. Static registers
(the version string) never expire.

Measured: **1 ms response latency, 10/10** once Venus's own probe services are out of the way.

### Four things that were bugs, kept as notes

- **Connect BY ADDRESS, never scan.** A connected BLE device stops advertising, so if a link is left
  open (service restarted mid-poll) a scan-based lookup can never find the battery again and the
  daemon locks itself out permanently. This happened.
- **Wait for a complete frame, don't sleep a fixed time.** A 0x03 response is ~41 bytes and arrives in
  several BLE notifications; a fixed delay is a race, and 0x03 went missing while the short 0x04
  always arrived.
- **Settle after `start_notify` and retry once.** The BMS is not ready to answer the instant
  notifications are subscribed, and the first register of each poll was intermittently lost.
- **The serial responder needs its OWN THREAD.** Sharing the asyncio loop with bleak means D-Bus
  round-trips stall the reader; a host polling every second would see timeouts.

### Testing against the Cerbo

Venus's `serial-starter` adopts the port and spawns `vedirect-interface.ttyACM0` and `gps-dbus.ttyACM0`
to probe it, and **it respawns them if you only stop the probes**. To test raw frames, stop the
supervisor too, then restore everything:

    svc -d /service/serial-starter /service/vedirect-interface.ttyACM0 /service/gps-dbus.ttyACM0
    ... test ...
    svc -u /service/serial-starter /service/vedirect-interface.ttyACM0 /service/gps-dbus.ttyACM0

In normal operation you do NOT fight serial-starter — dbus-serialbattery registers with it so the port
is assigned to the battery driver instead of probed.

## Historical: the USB-serial + dbus-serialbattery design (SUPERSEDED)

Kept because it documents real Venus behaviour and the reasons the CAN design replaced it.
**None of this is installed any more** — the driver, its `config.ini`, the udev pin and the GUI
overlay were all removed, and the Cerbo is back to stock.

Verified working 2026-09-14: `com.victronenergy.battery.ttyACM0` on dbus with full data
(voltage, current, SOC, capacity, cycles, temperature, per-cell voltages, alarms).

**Driver:** `dbus-serialbattery` v2.0.20250729, installed over SSH. Runs fine on **Venus OS v3.79**
even though the docs claim v2.9x–v3.6x, and its GUI v2 integration works too.

**`config.ini` (only the overrides; the rest comes from `config.default.ini`):**

    [DEFAULT]
    BMS_TYPE = LltJbd                      ; LltJbd IS the JBD family driver
    MAX_BATTERY_CHARGE_CURRENT = 50.0
    MAX_BATTERY_DISCHARGE_CURRENT = 80.0

**udev rule `/etc/udev/rules.d/99-bmsbridge.rules` — this one matters:**

    ACTION=="add", ENV{ID_BUS}=="usb", ENV{ID_VENDOR_ID}=="0525", ENV{ID_MODEL_ID}=="a4a7", ENV{VE_SERVICE}="sbattery"

Without it, Venus `serial-starter` also probes the port as **VE.Direct and GPS**. Those probes open the
same tty and consume responses meant for the battery driver, which looks exactly like a flaky link
(15/24 request failures in bursts). Pinning `VE_SERVICE` makes serial-starter assign the port instead
of guessing. **Note this rule lives on the rootfs and a Venus firmware update will wipe it**, along
with the root password and SSH keys.

**What the host actually asks for:** `dd a5 03 00 ff fd 77` — plain JBD read of register 0x03, once
per second, port held open. Nothing exotic. Capture it with an unbuffered Python reader on
`/dev/ttyGS0`; `od` buffers and shows nothing.

**If the link misbehaves, try a DIFFERENT CERBO USB PORT FIRST.** Every failure we saw was on `2-1.1`
behind the Cerbo's internal `0424:2422` hub — one-directional comms, then `error -110`, then no
enumeration. A direct port (`1-1`) has been solid.

**DVCC is now ON, and it was not a choice.** Presenting a managed CAN battery *forces* DVCC on
(`Services/Bol = 3`, `/Control/BmsParameters = 1`), so the limits in `canframes.py` are live control
values, not decoration: `/Control/EffectiveChargeVoltage` reads back our **14.2 V**. That is the
bottom of the Epoch's 14.2–14.4 V range, chosen deliberately. Change `CVL_V` only on purpose.

Also note the emulated battery becomes the system's **battery monitor** by auto-selection. When the
SmartShunt moves to the Cerbo, set *Settings → System setup → Battery monitor* to it explicitly,
otherwise a Pi or link failure takes the system's battery reference with it.

## Reading the battery from your own application

No root, no shell on the Cerbo. Two options, both GUI toggles on the Cerbo.

**Modbus TCP** — unit IDs come from `/opt/victronenergy/dbus-modbustcp/unitid2di.csv` on the device.
Device instances exceed the Modbus 1-byte unit-id field, and Victron already solved that with a
fixed map, so no `ModbusAlternates` fiddling is needed:

| unit | instance | device |
|---|---|---|
| **225** | 512 | CAN-bus BMS — this battery |
| 227 | 276 | Cerbo VE.Bus port — the MultiPlus |

    reg 259  uint16  /100   voltage
    reg 261  int16   /10    current
    reg 262  int16   /10    temperature
    reg 266  uint16  /10    SOC

**Reuse one connection for several registers.** Opening a socket per register gets some of them
refused, which looks exactly like missing data.

**MQTT** — topics are keyed on device instance, so the unit-id limit does not apply:
`N/<portalId>/battery/512/<path>`, payloads `{"value": …}`. Publish an empty message to
`R/<portalId>/keepalive` to make Venus republish everything. Values are 32-bit floats widened to
64-bit, so `13.16` arrives as `13.15999984741211` — round, never compare exactly.

**MQTT values are retained.** The broker returns the last known value whether or not the service is
alive, so MQTT alone cannot tell you the battery is still there. Modbus reads live at request time
and is the honest liveness check.

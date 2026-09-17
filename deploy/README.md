# Deployment files for `bmsbridge` (Pi Zero 2 W)

Copies of the files that live outside this repo on the bridge host. Kept here so the
install is reproducible, because losing any one of them breaks the link silently.

| file | goes to | why |
|---|---|---|
| `90-can0.rules` | `/etc/udev/rules.d/` | brings `can0` up at 500 kbit/s on boot **and on replug** |
| `vcan0.service` | `/etc/systemd/system/` | virtual CAN, for testing the emitter with no hardware |
| `../bms-bridge.service` | `/etc/systemd/system/` | the bridge itself |

## The Pi's USB port must be a HOST

The OTG port was in peripheral mode for the old gadget-serial link. For the USB-CAN
adapter it has to be a host:

- `/boot/firmware/config.txt`: comment out `dtoverlay=dwc2,dr_mode=peripheral` under `[all]`.
  (A `dtoverlay=dwc2,dr_mode=host` line may appear under `[cm5]` — that section does **not**
  apply to a Zero 2 W, so it is not the one that matters.)
- Disable `/etc/modules-load.d/gadget.conf`, which loads `g_serial`.
- Reboot. Confirm with: no `/sys/class/udc/`, no `g_serial` in `lsmod`, no `/dev/ttyGS0`,
  and a root hub visible in `lsusb` — that root hub is the quick proof of host mode.

Adapter: Innomaker USB2CAN, enumerates as `1d50:606f`, driver `gs_usb`, already in the
Raspberry Pi OS kernel. No driver install needed.

## Wiring, DB9 to the Cerbo's RJ45

**This pinout is Victron's.** Most other brands put CAN-H/L on pins 4/5 instead — check your
inverter's manual before wiring.

    DB9 7  ->  RJ45 7   CAN-H
    DB9 2  ->  RJ45 8   CAN-L
    DB9 3  ->  RJ45 3   GND

Terminate **both** ends: the jumper on the DB9 breakout, and a Victron RJ45 terminator in
the spare jack of the same VE.Can pair.

## Verifying after a restart — read this before diagnosing

A bridge restart takes a variable time to re-register on the Cerbo: 16 s once, still not
answering at 60 s another time. **Do not assume a fixed settle.** Poll until Modbus answers,
then read again ~20 s later — an *instant* answer is the tell that you are seeing the
pre-restart registration, which is how a working change once looked like a failure.

MQTT is not a liveness check on its own: `ProductName` and friends are **retained**, so the
broker keeps returning the last known value whether or not the service is alive.

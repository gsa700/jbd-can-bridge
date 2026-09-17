# Deployment files for `bmsbridge` (Pi Zero 2 W)

Copies of the files that live outside this repo on the bridge host. Kept here so the
install is reproducible, because losing any one of them breaks the link silently.

| file | goes to | why |
|---|---|---|
| `90-can0.rules` | `/etc/udev/rules.d/` | brings `can0` up at 500 kbit/s on boot **and on replug** |
| `vcan0.service` | `/etc/systemd/system/` | virtual CAN, for testing the emitter with no hardware |
| `../bms-bridge.service` | `/etc/systemd/system/` | the bridge itself |

## Troubleshooting: the adapter doesn't appear at all

**This should not happen on a normal Pi.** The USB port is a host by default, so the adapter
enumerates and `can0` shows up. `lsusb | grep 1d50:606f` and `dmesg | grep gs_usb` confirm it.

There is one situation that causes it, and it is worth knowing because the symptom is silence
rather than an error: **a Pi previously set up as a USB gadget** — USB ethernet, a serial console,
or anything else using `dwc2` with a gadget module — is in *peripheral* mode and cannot drive a
device at all. Check for `/sys/class/udc/` and `g_serial` in `lsmod`; if they are there, that is it.

To put such a Pi back to host mode:

- `/boot/firmware/config.txt`: comment out `dtoverlay=dwc2,dr_mode=peripheral` under `[all]`.
  (A `dtoverlay=dwc2,dr_mode=host` line may also appear under `[cm5]` in stock config files —
  that section does **not** apply to a Zero 2 W, so it is a red herring, not the line to change.)
- Disable whatever loads the gadget module, e.g. `/etc/modules-load.d/gadget.conf` for `g_serial`.
- Reboot. You are in host mode when `/sys/class/udc/` is gone and a **root hub appears in `lsusb`**
  — that root hub is the quick proof.

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

"""
Pylontech-profile CAN frames -- the published inverter-battery protocol.

Nothing in this module is vendor-specific: the frame IDs, the little-endian
encodings and the "PYLON" identifier are the Pylontech LV CAN protocol, which
Deye, Sol-Ark, SMA, Growatt, Goodwe, Luxpower and others consume unchanged.

It is TESTED against a Victron GX, which speaks it natively on a VE.Can port set
to "CAN-bus BMS (500 kbit/s)" -- no driver install, no root, no third-party
software on the GX. On Victron that is the whole point: everything GX-side stays
stock firmware. On other brands there was never anything to install.

Direction of travel is OUTBOUND ONLY. We never write to the battery; these frames
are built from values the BLE poller already read. The read-only rule is upstream
of this file and unaffected by it.

Encoding notes (verified against the Pylontech CAN spec and two independent
open-source emulators):
  - all multi-byte fields are LITTLE-ENDIAN
  - 0x351 limits are ADVISORY while DVCC is off; they become authoritative the
    moment DVCC is enabled, which is why they are explicit constants below

Frames:
  0x351  charge voltage limit, charge current limit, discharge current limit,
         discharge voltage limit
  0x355  SOC, SOH
  0x356  pack voltage, current, temperature
  0x359  protection + warning flags
  0x35C  charge/discharge enable
  0x35E  manufacturer name
"""
import struct

# ---------------------------------------------------------------- limits
# !! REVIEW BEFORE ENABLING DVCC !!
# While DVCC is off and the SmartShunt is the master battery monitor, these are
# informational. Turn DVCC on and the GX will obey them as hard limits.
# Epoch 12105A-H is a 4-cell 12 V 105 Ah LiFePO4; Epoch spec charge voltage is
# 14.2-14.4 V. We default to the BOTTOM of that range deliberately.
CVL_V   = 14.2    # charge voltage limit
CCL_A   = 50.0    # max charge current    (matches the value he ran in serialbattery)
DCL_A   = 80.0    # max discharge current (ditto)
DVL_V   = 11.0    # discharge voltage limit -- conservative LiFePO4 floor

# 0x35E identifies the battery to the GX. "PYLON   " is the canonical string and
# is what guarantees the GX recognises the profile on first contact. Once the link
# is proven, this can be changed to a friendlier name -- but change it ONLY after
# the battery has appeared correctly at least once, so a failure to appear is never
# ambiguous between "bad frames" and "unrecognised name".
MANUFACTURER = "PYLON   "

PACK_COUNT = 1

# ---------------------------------------------------------------- JBD protection word
# Register 0x03, offset 16, u16. Standard JBD/Jiabaida bit assignment.
JBD_CELL_OV      = 1 << 0
JBD_CELL_UV      = 1 << 1
JBD_PACK_OV      = 1 << 2
JBD_PACK_UV      = 1 << 3
JBD_CHG_OT       = 1 << 4
JBD_CHG_UT       = 1 << 5
JBD_DSG_OT       = 1 << 6
JBD_DSG_UT       = 1 << 7
JBD_CHG_OC       = 1 << 8
JBD_DSG_OC       = 1 << 9
JBD_SHORT        = 1 << 10
JBD_IC_ERROR     = 1 << 11
JBD_MOS_LOCK     = 1 << 12

# ---------------------------------------------------------------- Pylontech 0x359 bits
# byte 0 and byte 2 share a layout (errors / warnings respectively), as do 1 and 3.
P_OV      = 1 << 1   # cell over voltage
P_UV      = 1 << 2   # cell under voltage
P_OT      = 1 << 3   # over temperature
P_UT      = 1 << 4   # under temperature
P_DSG_OC  = 1 << 7   # discharge over current
P_CHG_OC  = 1 << 0   # (byte 1/3) charge over current
P_SYS_ERR = 1 << 3   # (byte 1/3) system error


def _clamp(v, lo, hi, counter=None, name=""):
    """Keep a value inside its wire type. A BLE glitch must never become a wild
    frame on the bus -- better to pin at the rail and count it."""
    if v < lo or v > hi:
        if counter is not None:
            counter.append((name, v))
        return lo if v < lo else hi
    return v


def _i16(x, counter=None, name=""):
    return int(_clamp(round(x), -32768, 32767, counter, name))


def _u16(x, counter=None, name=""):
    return int(_clamp(round(x), 0, 65535, counter, name))


def alarm_bytes(protection: int):
    """Map the JBD protection word onto Pylontech's 0x359 error bytes.

    Only genuine protection events are mapped. JBD does not expose a separate
    'warning' tier, so the warning bytes stay clear rather than inventing a
    threshold here -- a fabricated warning is worse than none.
    """
    b0 = b1 = 0
    if protection & (JBD_CELL_OV | JBD_PACK_OV):        b0 |= P_OV
    if protection & (JBD_CELL_UV | JBD_PACK_UV):        b0 |= P_UV
    if protection & (JBD_CHG_OT | JBD_DSG_OT):          b0 |= P_OT
    if protection & (JBD_CHG_UT | JBD_DSG_UT):          b0 |= P_UT
    if protection & (JBD_DSG_OC | JBD_SHORT):           b0 |= P_DSG_OC
    if protection & JBD_CHG_OC:                         b1 |= P_CHG_OC
    if protection & (JBD_IC_ERROR | JBD_MOS_LOCK):      b1 |= P_SYS_ERR
    return b0, b1


def build_frames(d: dict, soh=100, clamps=None):
    """Build the six frames from a decoded BLE snapshot.

    Returns [(can_id, bytes), ...], or [] if the snapshot lacks the fields that
    make a frame meaningful. Emitting zeros for a battery we cannot read would
    look to the GX like a real 0 V battery, so we emit nothing instead.
    """
    required = ("pack_voltage_v", "current_a", "soc_percent")
    if not all(k in d for k in required):
        return []

    c = clamps if clamps is not None else []
    out = []

    # 0x351 -- limits. Constants, not measurements: they describe the battery's
    # ratings, not its current state.
    out.append((0x351, struct.pack("<HhhH",
        _u16(CVL_V * 10, c, "cvl"),
        _i16(CCL_A * 10, c, "ccl"),
        _i16(DCL_A * 10, c, "dcl"),
        _u16(DVL_V * 10, c, "dvl"))))

    # 0x355 -- SOC / SOH
    out.append((0x355, struct.pack("<HH",
        _u16(d["soc_percent"], c, "soc"),
        _u16(soh, c, "soh"))))

    # 0x356 -- live measurements. Temperature is the mean of whatever NTCs the
    # pack reported; absent sensors mean we send 0, which Victron treats as
    # "no reading" rather than as freezing.
    temps = [v for k, v in d.items() if k.startswith("temp") and k.endswith("_c")]
    temp = sum(temps) / len(temps) if temps else 0.0
    out.append((0x356, struct.pack("<hhh",
        _i16(d["pack_voltage_v"] * 100, c, "voltage"),
        _i16(d["current_a"] * 10, c, "current"),
        _i16(temp * 10, c, "temp"))))

    # 0x359 -- alarms. byte 4 is the module count, bytes 5-6 the literal "PN".
    b0, b1 = alarm_bytes(d.get("protection", 0))
    out.append((0x359, bytes([b0, b1, 0x00, 0x00, PACK_COUNT, 0x50, 0x4E, 0x00])))

    # 0x35C -- charge/discharge enable, taken from the BMS's own FET states so a
    # BMS that has opened a FET is reflected upstream rather than contradicted.
    flags = 0
    if d.get("fet_charge", 1):    flags |= 0x80
    if d.get("fet_discharge", 1): flags |= 0x40
    out.append((0x35C, bytes([flags, 0x00])))

    # 0x35E -- manufacturer, exactly 8 ASCII bytes
    out.append((0x35E, MANUFACTURER.encode("ascii")[:8].ljust(8, b"\x00")))

    return out

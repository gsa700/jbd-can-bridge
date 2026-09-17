#!/usr/bin/env python3
"""bms-bridge — read the Epoch's JBD BMS over BLE, re-serve it to the Cerbo over
USB-gadget serial, and expose Prometheus metrics on the way past.

WHY THIS EXISTS
  The Epoch 12105A-H exposes no serial and no CAN -- Bluetooth LE only. Venus OS
  CAN read JBD over BLE via dbus-serialbattery, but that project's own docs say
  "the Bluetooth connection is still not stable on some systems; if you want a
  stable connection use the serial connection." So this moves the flaky radio
  onto a $15 Pi and hands the Cerbo the connection type its maintainers call
  stable. The JBD frame format is IDENTICAL over both transports, so this is a
  TRANSPORT SWAP, not a translation.

THREE PROPERTIES THAT MATTER

1. READ ONLY, ENFORCED IN BOTH DIRECTIONS.
   Toward the battery: the only frame builder here hard-codes 0xA5 (read). No
   function exists that can emit 0x5A (write-register), so no code path can
   change a BMS setting or enter factory mode.
   Toward the Cerbo: any inbound frame that is NOT a 0xA5 read is DROPPED and
   counted, never forwarded. The bridge is a read-only firewall -- even if
   something upstream tried to write settings, it physically cannot get through.

2. CACHE-AND-SERVE, NOT A TRANSPARENT PROXY.
   The BLE poll runs on its own clock. Serial requests are answered instantly
   from cache. A transparent proxy would inherit BLE's latency and dropouts, and
   the Cerbo's serial reads would time out whenever the radio hiccuped -- making
   the battery vanish from Victron. Here a stall becomes stale data instead.

3. STALENESS IS BOUNDED, AND WE STOP LYING RATHER THAN LIE FOREVER.
   Cached frames are served while younger than MAX_SERVE_AGE, which rides out a
   couple of missed polls. Past that we STOP RESPONDING, so the Cerbo marks the
   battery absent rather than acting on data that is minutes old. That matters
   because dbus-serialbattery can feed charge limits into DVCC.

CACHED FRAMES ARE REPLAYED VERBATIM. We store exactly what the BMS said,
checksums and all, and hand the same bytes to the Cerbo. Nothing is re-encoded,
so there is no opportunity for us to corrupt a field we misunderstood.
(Register 0x0f returns everything in one read and is lovely for a metrics-only
poller -- but a bridge must replay the registers the host actually asks for, so
0x03 and 0x04 are what we poll.)
"""
import asyncio, os, select, sys, termios, time, threading, http.server, socketserver, logging

# ---------------------------------------------------------------- configuration
BMS_MAC        = os.environ.get("BMS_MAC", "")   # REQUIRED: set to your BMS's BLE address
SERIAL_DEV     = os.environ.get("BMS_SERIAL", "/dev/ttyGS0")
POLL_INTERVAL  = float(os.environ.get("BMS_POLL_INTERVAL", "15"))   # seconds between BLE polls
MAX_SERVE_AGE  = float(os.environ.get("BMS_MAX_SERVE_AGE", "60"))   # stop answering past this
METRICS_PORT   = int(os.environ.get("BMS_METRICS_PORT", "9101"))
DEBUG_REQ      = os.environ.get("BMS_DEBUG_REQ", "") not in ("", "0")
CAN_IFACE      = os.environ.get("BMS_CAN", "")                      # "" = CAN emitter off
CAN_INTERVAL   = float(os.environ.get("BMS_CAN_INTERVAL", "1.0"))   # Pylontech cadence
CAN_MAX_AGE    = float(os.environ.get("BMS_CAN_MAX_AGE", "60"))     # stop emitting past this

CH_TX = "0000ff02-0000-1000-8000-00805f9b34fb"   # we write requests here
CH_RX = "0000ff01-0000-1000-8000-00805f9b34fb"   # answers arrive here
CMD_READ = 0xA5                                   # 0x5A (write) is deliberately absent everywhere
POLL_REGS   = (0x03, 0x04)     # live data; re-read every poll, replayed verbatim
STATIC_REGS = (0x05,)          # version string: read once, NEVER expires (see below)

log = logging.getLogger("bms-bridge")

def read_frame(reg: int) -> bytes:
    """Build a READ request. Takes a register only -- no payload can be attached,
    which is what makes a write-register frame unconstructible."""
    chk = (0x10000 - reg) & 0xFFFF
    return bytes([0xDD, CMD_READ, reg, 0x00, chk >> 8, chk & 0xFF, 0x77])

# ---------------------------------------------------------------- shared state
class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = {}          # reg -> (raw_response_bytes, monotonic_timestamp)
        self.decoded = {}         # human/metric-friendly values
        self.stats = dict(polls_ok=0, polls_failed=0, frames_served=0,
                          frames_stale=0, writes_refused=0, unknown_reqs=0,
                          ble_connects=0, ble_errors=0,
                          can_cycles=0, can_errors=0, can_suppressed=0, can_clamped=0)
    def put(self, reg, frame):
        with self.lock:
            self.frames[reg] = (frame, time.monotonic())
    def get(self, reg, max_age):
        with self.lock:
            e = self.frames.get(reg)
            if not e: return None, None
            frame, ts = e
            return frame, time.monotonic() - ts
    def bump(self, k, n=1):
        with self.lock: self.stats[k] = self.stats.get(k, 0) + n
    def snapshot(self):
        with self.lock:
            ages = {r: time.monotonic() - ts for r, (_, ts) in self.frames.items()}
            return dict(self.stats), dict(self.decoded), ages

CACHE = Cache()

# ---------------------------------------------------------------- BLE decoding
def u16(b, i): return (b[i] << 8) | b[i+1]
def s16(b, i):
    v = u16(b, i); return v - 0x10000 if v & 0x8000 else v

def decode(frames):
    """Decode for METRICS only. The Cerbo gets raw frames, never this."""
    out = {}
    f = frames.get(0x03)
    if f and len(f) > 4 and f[2] == 0:
        d = f[4:4+f[3]]
        try:
            out.update(
                pack_voltage_v = u16(d, 0) / 100.0,
                current_a      = s16(d, 2) / 100.0,
                residual_ah    = u16(d, 4) / 100.0,
                nominal_ah     = u16(d, 6) / 100.0,
                cycles         = u16(d, 8),
                protection     = u16(d, 16),
                soc_percent    = d[19],
                fet_charge     = 1 if d[20] & 1 else 0,
                fet_discharge  = 1 if d[20] & 2 else 0,
                cell_count     = d[21],
            )
            ntc = d[22]
            for i in range(ntc):
                out[f"temp{i+1}_c"] = (u16(d, 23 + i*2) - 2731) / 10.0
        except IndexError:
            pass
    f = frames.get(0x04)
    if f and len(f) > 4 and f[2] == 0:
        d = f[4:4+f[3]]
        mv = [u16(d, i*2) for i in range(len(d)//2)]
        if mv:
            for i, v in enumerate(mv): out[f"cell{i+1}_mv"] = v
            out["cell_spread_mv"] = max(mv) - min(mv)
            out["cell_min_mv"], out["cell_max_mv"] = min(mv), max(mv)
    return out

def split_frames(buf: bytearray):
    out = []
    while len(buf) >= 7 and buf[0] == 0xDD:
        total = buf[3] + 7
        if len(buf) < total: break
        out.append(bytes(buf[:total])); del buf[:total]
    return out

# ---------------------------------------------------------------- BLE poller
async def ble_poller():
    from bleak import BleakClient
    import subprocess

    if not BMS_MAC:
        log.error("BMS_MAC is not set. Find your BMS with ble-scan.py (match on "
                  "service UUID 0xff00, NOT the advertised name) and set BMS_MAC.")
        return

    # Clear any link BlueZ is still holding from a previous run. systemd can
    # replace this process while a poll is in flight, and BlueZ then keeps the
    # connection open on behalf of a PID that no longer exists. The battery
    # stops advertising while connected, so every later attempt fails with
    # "device not found" and the daemon never recovers without a manual
    # bluetoothctl disconnect. Cheap to do, and only ever a no-op.
    try:
        subprocess.run(["bluetoothctl", "disconnect", BMS_MAC],
                       capture_output=True, timeout=10)
    except Exception as e:
        log.debug("startup disconnect skipped: %s", e)

    got_once = False
    while True:
        try:
            # Connect BY ADDRESS, do not scan first.
            #
            # A connected BLE device STOPS ADVERTISING. So if a previous
            # connection is left open -- e.g. the service is restarted mid-poll
            # and BlueZ keeps the link -- a scan-based lookup can never find the
            # battery again, and the daemon locks itself out permanently.
            # Connecting by address sidesteps that entirely, and also skips
            # several seconds of scanning every cycle.
            dev = BMS_MAC
            buf = bytearray()
            def on_notify(_, data): buf.extend(data)

            # Connect, read, DISCONNECT. Holding the link open would lock his
            # phone app out -- most BMS BLE stacks allow only one connection.
            async with BleakClient(dev, timeout=20.0) as cl:
                CACHE.bump("ble_connects")
                await cl.start_notify(CH_RX, on_notify)
                # Let the link settle before the first request. Without this the
                # first register of each poll was intermittently lost on rapid
                # reconnects -- the BMS is not ready to answer the instant
                # notifications are subscribed.
                await asyncio.sleep(0.5)
                regs = list(POLL_REGS) + ([] if got_once else list(STATIC_REGS))
                fresh = {}
                for reg in regs:
                  for attempt in (1, 2):          # one retry: cheap, and BLE is lossy
                    buf.clear()
                    await cl.write_gatt_char(CH_TX, read_frame(reg), response=False)
                    # Wait for a COMPLETE frame instead of sleeping a fixed time.
                    # A 0x03 response is ~41 bytes, which exceeds one BLE
                    # notification, so it arrives in several fragments. A fixed
                    # delay is a race -- and it lost: 0x03 would intermittently
                    # go missing while the short 0x04 always made it.
                    got, deadline = None, time.monotonic() + 3.0
                    while time.monotonic() < deadline and got is None:
                        await asyncio.sleep(0.1)
                        for fr in split_frames(bytearray(buf)):   # copy: don't consume
                            if fr[1] == reg and fr[2] == 0:
                                got = fr; break
                    if got:
                        CACHE.put(reg, got); fresh[reg] = got
                        break
                    log.info("reg 0x%02x no answer (attempt %d)", reg, attempt)
                await cl.stop_notify(CH_RX)

            if all(r in fresh for r in POLL_REGS):
                got_once = True
                CACHE.bump("polls_ok")
                with CACHE.lock:
                    CACHE.decoded = decode({r: f for r, (f, _) in CACHE.frames.items()})
                log.info("poll ok: %s", ", ".join(
                    f"{k}={v}" for k, v in sorted(CACHE.decoded.items())
                    if k in ("pack_voltage_v", "soc_percent", "cell_spread_mv", "protection")))
            else:
                CACHE.bump("polls_failed")
                log.warning("incomplete poll, got registers %s", sorted(fresh))
        except Exception as e:
            CACHE.bump("ble_errors"); CACHE.bump("polls_failed")
            log.warning("BLE poll failed: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------------------------------------------------------- serial server
def open_serial(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0                       # iflag, oflag, lflag: raw
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[6][termios.VMIN] = 0; a[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
    except termios.error as e:
        # Gadget serial ignores most termios; not fatal.
        log.info("termios on %s: %s (harmless on a gadget tty)", path, e)
    return fd

def serial_thread():
    """Serve JBD frames on the gadget tty, in its OWN THREAD.

    This deliberately does NOT share the asyncio loop with the BLE poller.
    bleak's D-Bus round-trips can stall the loop for a second or two, and while
    stalled the serial reader never fires -- a host polling once a second would
    see intermittent timeouts and decide the battery had gone away. The entire
    point of cache-and-serve is that the serial side answers instantly no matter
    what the radio is doing, so the two must not share a scheduler. They share
    only the lock-protected cache.
    """
    fd = open_serial(SERIAL_DEV)
    log.info("serving JBD frames on %s (own thread, port held open)", SERIAL_DEV)
    buf = bytearray()

    def handle_request(frame: bytes):
        cmd, reg = frame[1], frame[2]
        if cmd != CMD_READ:
            # THE READ-ONLY FIREWALL. A write-register frame from upstream is
            # dropped here and never reaches the battery.
            CACHE.bump("writes_refused")
            log.warning("REFUSED non-read frame cmd=0x%02x reg=0x%02x -- not forwarded", cmd, reg)
            return
        cached, age = CACHE.get(reg, MAX_SERVE_AGE)
        if cached is None:
            CACHE.bump("unknown_reqs")
            if DEBUG_REQ: log.info("UNKNOWN reg 0x%02x requested -- not cached", reg)
            return
        # Static registers never expire; the version string cannot go out of date.
        if reg not in STATIC_REGS and age > MAX_SERVE_AGE:
            # Stop answering rather than serve minutes-old data the Cerbo might
            # feed into DVCC. Silence makes the battery go absent, which is the
            # honest failure.
            CACHE.bump("frames_stale")
            log.warning("reg 0x%02x is %.0fs stale (>%.0f) -- not answering", reg, age, MAX_SERVE_AGE)
            return
        try:
            os.write(fd, cached); CACHE.bump("frames_served")
            if DEBUG_REQ: log.info("served reg 0x%02x (%d bytes, age %.1fs)", reg, len(cached), age)
        except OSError as e:
            log.warning("serial write failed: %s", e)

    while True:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            try:
                data = os.read(fd, 512)
            except BlockingIOError:
                continue
            if not data:
                continue
            buf.extend(data)
            while True:
                start = buf.find(0xDD)
                if start < 0:
                    buf.clear(); break
                if start: del buf[:start]
                if len(buf) < 7: break
                if buf[1] == CMD_READ:
                    frame = bytes(buf[:7])
                    if frame[6] != 0x77:
                        del buf[0]; continue      # false start, resync
                    del buf[:7]; handle_request(frame)
                else:
                    handle_request(bytes(buf[:7]))   # refuse, then resync past it
                    del buf[0]
        except OSError as e:
            log.warning("serial loop error: %s", e); time.sleep(1)

# ---------------------------------------------------------------- metrics
def can_thread():
    """Emit Pylontech-profile frames so a stock Victron GX sees a managed battery.

    Outbound only. Nothing read from the bus is ever acted on -- we do not even
    open a receive filter -- so this cannot become a path back to the BMS.
    """
    import can, canframes
    log.info("CAN emitter starting on %s at %.1f Hz", CAN_IFACE, 1.0 / CAN_INTERVAL)
    bus = None
    while True:
        try:
            if bus is None:
                bus = can.Bus(channel=CAN_IFACE, interface="socketcan")
                log.info("CAN bus %s open", CAN_IFACE)

            # Freshness gate: same rule as the serial responder. A battery we
            # cannot currently read must go silent, not keep asserting old values.
            _, decoded, ages = CACHE.snapshot()
            age = ages.get(0x03)
            if age is None or age > CAN_MAX_AGE:
                CACHE.bump("can_suppressed")
                time.sleep(CAN_INTERVAL); continue

            clamps = []
            frames = canframes.build_frames(decoded, clamps=clamps)
            if not frames:
                CACHE.bump("can_suppressed")
                time.sleep(CAN_INTERVAL); continue

            for cid, data in frames:
                bus.send(can.Message(arbitration_id=cid, data=data,
                                     is_extended_id=False))
            CACHE.bump("can_cycles")
            if clamps:
                CACHE.bump("can_clamped", len(clamps))
                log.warning("CAN value(s) clamped: %s", clamps)

        except Exception as e:
            CACHE.bump("can_errors")
            log.warning("CAN emitter: %s", e)
            try:
                if bus: bus.shutdown()
            except Exception:
                pass
            bus = None
            time.sleep(2)
            continue
        time.sleep(CAN_INTERVAL)


class Metrics(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        stats, dec, ages = CACHE.snapshot()
        L = []
        # NOTE: `L.extend(...)`, not `L += ...`. Inside a nested function, `L +=`
        # is an assignment to a local and raises UnboundLocalError -- which took
        # the whole metrics endpoint down with a bare connection reset.
        def g(name, help_, val, labels="", typ="gauge"):
            if val is None: return
            L.extend([f"# HELP {name} {help_}", f"# TYPE {name} {typ}",
                      f"{name}{labels} {val}"])
        for k, v in sorted(dec.items()):
            g(f"epoch_bms_{k}", f"Epoch BMS {k.replace('_',' ')}", v)
        # HELP/TYPE are emitted once for the family; labels go on the sample only.
        if ages:
            L.extend(["# HELP epoch_bms_register_age_seconds Seconds since this register was last read from the BMS",
                      "# TYPE epoch_bms_register_age_seconds gauge"])
            for r, a in sorted(ages.items()):
                L.append(f'epoch_bms_register_age_seconds{{register="0x{r:02x}"}} {round(a, 1)}')
        for k, v in sorted(stats.items()):
            g(f"epoch_bms_{k}_total", f"Bridge counter: {k}", v, typ="counter")
        body = "\n".join(L) + "\n"
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

def metrics_server():
    class S(socketserver.ThreadingTCPServer): allow_reuse_address = True
    with S(("", METRICS_PORT), Metrics) as srv:
        log.info("metrics on :%d", METRICS_PORT)
        srv.serve_forever()

# ---------------------------------------------------------------- main
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=metrics_server, daemon=True).start()
    # The serial responder is legacy: it re-served JBD frames to the Cerbo over
    # USB-gadget serial, before the Cerbo moved to native CAN. Keep the code --
    # it still works if a gadget tty is present -- but do not start the thread
    # when the device is absent or BMS_SERIAL is empty. Otherwise the thread
    # dies with an unhandled FileNotFoundError on every boot, which is noise
    # that would hide a real fault later.
    if SERIAL_DEV and os.path.exists(SERIAL_DEV):
        threading.Thread(target=serial_thread, daemon=True).start()
    else:
        log.info("serial responder disabled (%s)",
                 "BMS_SERIAL empty" if not SERIAL_DEV else f"no {SERIAL_DEV}")
    if CAN_IFACE:
        threading.Thread(target=can_thread, daemon=True).start()
    await ble_poller()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

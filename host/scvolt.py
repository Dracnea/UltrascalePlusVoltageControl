#!/usr/bin/env python3
"""The C1100 satellite-controller protocol — codec and command set.

Stdlib only, and NOTHING HERE TOUCHES HARDWARE: it builds bytes and parses
bytes. The transport lives elsewhere (sclink.py, over the scbridge BSCAN register); this is
the half that can be verified without a card, and it is verified — `selfcheck()`
reproduces, byte for byte, the complete session TeamRedMiner ran on a C1100 while
setting VCCINT to 720 mV, captured with usbmon. See ../docs/protocol.md.

Protocol reference: ../docs/protocol.md. The command ids here (0x03
identify, 0x04 comm version, 0x05 sensors, 0x70 set rails, 0x71 firmware
version) are TRM's custom set and exist only on a TRM-flashed satellite
controller. AMD documents the SC↔FPGA UART as "a proprietary protocol" and
publishes no command that sets a rail, so a stock SC NAKs all of this — which is
the expected, harmless outcome, not a fault.

    5c 02 | cmd | 00 | len | data[len] | sum16 LE over cmd..data | 5c 03
            \\________________ 0x5c doubled in here ______________/
"""
import struct

STX = b"\x5c\x02"
ETX = b"\x5c\x03"

# --- commands ---------------------------------------------------------------
IDENTIFY   = 0x03   # -> 0x83   open session, first thing sent
COMM_VER   = 0x04   # -> 0xFE   one data byte, min(ver, 9)
SENSORS    = 0x05   # -> 0x85   read all sensors
SET_RAILS  = 0x70   # -> --     1..3 records of [rail][mV_lo][mV_hi]
FW_VERSION = 0x71   # -> 0x72   TRM firmware version; a stock SC never answers
POLL       = (0x06, 0x07, 0x08)
NAK        = 0xFF
# 0x01 enters the TI BSL to flash firmware. Deliberately not implemented.

RAIL_VCCINT  = 0x10
RAIL_VCCBRAM = 0x36
RAIL_VCCMEM  = 0x34
RAIL_NAME = {RAIL_VCCINT: "VCCINT", RAIL_VCCBRAM: "VCCBRAM", RAIL_VCCMEM: "VCCMEM"}

# Floors are gated by SC firmware version, NOT by TRM's 600 mV clamp, which is
# software and bypassable. See docs/sc-protocol.md.
FLOOR_MV = {           # fw -> (vccint, vccbram, vccmem)
    "1.3":   (500, 700, 1050),
    "1.2":   (625, 700, 1050),
    "stock": (675, 700, 1100),
}
CEILING_MV = (900, 950, 1350)   # never exceeded by this tool; overvolt is the
                                # direction that destroys the part
DEFAULT_MV = (800, 850, 1200)   # the C1100's stock setpoints, in rail order

RAIL_ORDER = (RAIL_VCCINT, RAIL_VCCBRAM, RAIL_VCCMEM)


def level(word, idx, fw="1.3"):
    """Resolve a rail argument: a number, `min`, or `default`.

    `min` is the point of this: a design that instantiates no BRAM and no HBM
    has no reason to hold those rails at 850/1200 mV. The BLAKE3 farm is exactly
    that case -- 0 BRAM tiles, 0 URAM, no HBM -- so both memory rails can sit on
    the floor while VCCINT does the work.
    """
    if isinstance(word, int):
        return word
    w = str(word).strip().lower()
    if w in ("min", "floor"):
        return FLOOR_MV[fw][idx]
    if w in ("default", "stock"):
        return DEFAULT_MV[idx]
    return int(w)


def _escape(body):
    return body.replace(b"\x5c", b"\x5c\x5c")


def _unescape(body):
    return body.replace(b"\x5c\x5c", b"\x5c")


def checksum(body):
    """16-bit sum over cmd .. data, little-endian on the wire."""
    return sum(body) & 0xFFFF


def frame(cmd, data=b""):
    """Build one SC frame."""
    if len(data) > 0xFF:
        raise ValueError("payload too long")
    body = bytes([cmd, 0x00, len(data)]) + bytes(data)
    body += struct.pack("<H", checksum(body))
    return STX + _escape(body) + ETX


def parse(raw):
    """-> (cmd, data) for one complete frame, or raise."""
    if not (raw.startswith(STX) and raw.endswith(ETX)):
        raise ValueError("not a framed message")
    body = _unescape(raw[2:-2])
    if len(body) < 5:
        raise ValueError("runt frame")
    cmd, zero, ln = body[0], body[1], body[2]
    data, got = body[3:3 + ln], struct.unpack("<H", body[3 + ln:5 + ln])[0]
    want = checksum(body[:3 + ln])
    if got != want:
        raise ValueError("checksum %04x, expected %04x" % (got, want))
    return cmd, data


# --- the commands a voltage tool actually needs ------------------------------
def identify():        return frame(IDENTIFY)
def comm_version(v=9): return frame(COMM_VER, bytes([min(v, 9)]))
def sensors():         return frame(SENSORS)
def fw_version():      return frame(FW_VERSION)


def set_rails(vccint_mv, vccbram_mv=None, vccmem_mv=None, fw="1.3"):
    """Build the 0x70 write.

    TRM always sends all three rails, carrying the two it is not changing at
    their present values — never a lone VCCINT record. A tool that sends one
    record is doing something TRM has never done and nobody has tested, so pass
    the other two (read them back with SENSORS first) rather than omitting them.
    """
    floors = FLOOR_MV[fw]
    recs, want = b"", ((RAIL_VCCINT, vccint_mv), (RAIL_VCCBRAM, vccbram_mv),
                       (RAIL_VCCMEM, vccmem_mv))
    for i, (rail, mv) in enumerate(want):
        if mv is None:
            continue
        if not (floors[i] <= mv <= CEILING_MV[i]):
            raise ValueError("%s %d mV outside %d..%d for SC firmware %s"
                             % (RAIL_NAME[rail], mv, floors[i], CEILING_MV[i], fw))
        recs += bytes([rail]) + struct.pack("<H", mv)
    if not recs:
        raise ValueError("no rails given")
    return frame(SET_RAILS, recs)


# --- verification against the wire -------------------------------------------
# Captured 2026-09-13 with usbmon while TRM set VCCINT to 720 mV on this card.
CAPTURED = [
    ("identify",     "5c02030000" "0300" "5c03",                       identify),
    ("comm version", "5c0204000109" "0e00" "5c03",                     lambda: comm_version(9)),
    ("sensors",      "5c02050000" "0500" "5c03",                       sensors),
    ("fw version",   "5c02710000" "7100" "5c03",                       fw_version),
    ("set rails",    "5c02700009" "10d002" "365203" "34b004" "ce02" "5c03",
                     lambda: set_rails(720, 850, 1200)),
]
# The only real SC frame this project ever captured before the 2026-09-13 run:
# a stock VU9P answering NAK. Independent confirmation of the framing.
VU9P_NAK = "5c02ff000200040501" "5c03"


def selfcheck():
    ok = True
    for name, hexed, build in CAPTURED:
        want, got = bytes.fromhex(hexed), build()
        flag = "ok " if got == want else "FAIL"
        ok &= got == want
        print(f"  {flag} {name:<13} {got.hex()}")
        if got != want:
            print(f"       expected {want.hex()}")
    cmd, data = parse(bytes.fromhex(VU9P_NAK))
    good = cmd == NAK and data == b"\x00\x04"
    ok &= good
    print(f"  {'ok ' if good else 'FAIL'} VU9P NAK      cmd=0x{cmd:02x} data={data.hex()}")

    for bad, why in ((499, "below the 1.3 floor"), (950, "above the ceiling")):
        try:
            set_rails(bad)
            print(f"  FAIL {bad} mV accepted ({why})"); ok = False
        except ValueError:
            print(f"  ok  refused {bad} mV ({why})")
    print("ALL FRAMES REPRODUCE" if ok else "MISMATCH")
    return 0 if ok else 1


def describe(fw="1.3"):
    lo, hi = FLOOR_MV[fw], CEILING_MV
    for i, rail in enumerate(RAIL_ORDER):
        print(f"  {RAIL_NAME[rail]:<8} floor {lo[i]:>5} mV   default {DEFAULT_MV[i]:>5} mV"
              f"   ceiling {hi[i]:>5} mV")


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="Build the satellite-controller frame that sets the C1100's rails.",
        epilog="Each rail takes a millivolt number, `min` (the SC firmware's floor) "
               "or `default` (the card's stock setpoint). A design with no BRAM and "
               "no HBM should run both memory rails at min.")
    ap.add_argument("--vccint",  default=None, help="core rail, mV | min | default")
    ap.add_argument("--vccbram", default=None, help="BRAM rail, mV | min | default")
    ap.add_argument("--vccmem",  default=None, help="HBM/mem rail, mV | min | default")
    ap.add_argument("--fw", default="1.3", choices=sorted(FLOOR_MV),
                    help="SC firmware version; it gates the floors (default 1.3)")
    ap.add_argument("--selfcheck", action="store_true", help="verify against the wire capture")
    ap.add_argument("--limits", action="store_true", help="print the rail limits and exit")
    a = ap.parse_args(argv)

    if a.selfcheck:
        return selfcheck()
    if a.limits or not any((a.vccint, a.vccbram, a.vccmem)):
        print(f"SC firmware {a.fw}:")
        describe(a.fw)
        if not a.limits:
            print("\nnothing to build: give at least one rail")
            return 2
        return 0

    mv = [None, None, None]
    for i, word in enumerate((a.vccint, a.vccbram, a.vccmem)):
        if word is not None:
            mv[i] = level(word, i, a.fw)
    try:
        f = set_rails(mv[0], mv[1], mv[2], fw=a.fw)
    except ValueError as e:
        print("REFUSED:", e)
        return 2

    for i, rail in enumerate(RAIL_ORDER):
        if mv[i] is not None:
            print(f"  {RAIL_NAME[rail]:<8} -> {mv[i]} mV")
    missing = [RAIL_NAME[r] for i, r in enumerate(RAIL_ORDER) if mv[i] is None]
    if missing:
        print("  NOTE: %s not in this frame. TeamRedMiner always writes all three,"
              % ", ".join(missing))
        print("        carrying the unchanged rails at their present values. Read them")
        print("        back with SENSORS (0x05) and echo them rather than omitting them.")
    print("\nframe:", f.hex())
    print("(frame only -- scset.py sends it and verifies with SYSMON)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Set a TUL TH53 / TH55's VCCINT through the vrmbridge bitstream, and prove it.

    vrmset.py --vccint 750            ramp the core rail to 750 mV
    vrmset.py --vccint default        back to the board's shipped setpoint
    vrmset.py --show                  read everything, change nothing
    vrmset.py --limits                the limits this tool enforces
    vrmset.py --check --vccint 750    validate a request; no hardware
    vrmset.py --selftest              exercise the logic against a fake VRM

The bridge bitstream must already be loaded -- changeVoltage.sh does that.

HOW THESE BOARDS DIFFER FROM THE C1100
    There is no satellite controller in the rail path. VCCINT comes from an
    Infineon PMBus regulator at 7-bit address 0x60, wired to fabric pins L12
    (SCL) / L13 (SDA) in bank 67 on both boards, so the bridge is a plain I2C
    master and this script speaks PMBus to the regulator directly. Only VCCINT
    is reachable this way; there is no VCCBRAM or VCCMEM control.

    VOUT_COMMAND and READ_VOUT on this regulator carry millivolts as a plain
    integer (720 = 0.720 V), not PMBus LINEAR16. That was measured on both
    boards, and this script re-proves it on every run before writing:
    the regulator's own READ_VOUT has to agree with SYSMON on the die, and the
    setpoint with READ_VOUT, or nothing is written.

SAFETY
    - Overvolting destroys the part; undervolting only makes the fabric go
      quiet until it is reprogrammed. CEILING_MV is not overridable, and the
      regulator's own VOUT_MAX and over-voltage fault limit lower it further.
    - The floor keeps clear of the regulator's under-voltage fault and
      power-good thresholds: tripping either can turn the rail OFF, which on
      these boards means a power cycle, not a reprogram.
    - The rail moves in steps of at most STEP_MV, each one read back.
    - WRITE_PROTECT is never changed. If the regulator is write-protected,
      this refuses.
    - The setpoint is global and persists across reconfiguration, so whatever
      you load next inherits it.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- the bridge register (rtl/vrmbridge_top.sv) ------------------------------
OP_NOP, OP_SYSMON, OP_LINES, OP_PMBUS = 0x00, 0x01, 0x05, 0x08
BRIDGE_ID = 0x5642                       # "VB" in result[63:48]

M_QUICK, M_RBYTE, M_WBYTE, M_RWORD, M_WWORD, M_SEND = 0, 1, 2, 3, 4, 5

# --- the regulator ------------------------------------------------------------
VRM_ADDR = 0x60
PAGE, CLEAR_FAULTS, WRITE_PROTECT, VOUT_MODE = 0x00, 0x03, 0x10, 0x20
VOUT_COMMAND, VOUT_MAX = 0x21, 0x24
VOUT_OV_FAULT_LIMIT, VOUT_UV_FAULT_LIMIT = 0x40, 0x44
POWER_GOOD_ON, POWER_GOOD_OFF = 0x5E, 0x5F
STATUS_WORD, READ_VOUT, READ_IOUT, READ_TEMPERATURE_1 = 0x79, 0x8B, 0x8C, 0x8D
READ_POUT, PMBUS_REVISION = 0x96, 0x98

# STATUS_WORD bits that mean the regulator is unhappy with the output.
STATUS_FAULT_MASK = (1 << 15) | (1 << 14) | (1 << 5) | (1 << 4) | (1 << 2)

# --- limits -------------------------------------------------------------------
# VCCINT's recommended maximum in DS923 for UltraScale+ parts run at 0.85 V is
# 0.876 V. Nothing this tool exists for needs more, and above it is the
# direction that destroys silicon. Not overridable.
CEILING_MV = 876
# Undervolting is the safe direction, but these boards have only been run
# down to 650 mV and the regulator's power-good/UV thresholds sit below that.
# 600 is a policy floor; the regulator's own thresholds may raise it.
FLOOR_MV = 600
STEP_MV = 20
AGREE_MV = 25            # READ_VOUT vs SYSMON, and setpoint vs READ_VOUT
CONFIRM_MV = 15          # SYSMON after vs commanded

BOARDS = {
    "th53": {"name": "TUL TH53 (xcvu33p)", "part": "*vu33p*", "default": 700,
             "bit": "vrmbridge_th53.bit"},
    "th55": {"name": "TUL TH55 (xcvu35p)", "part": "*vu35p*", "default": 720,
             "bit": "vrmbridge_th55.bit"},
}


def board_for_part(part):
    import fnmatch
    for key, b in BOARDS.items():
        if fnmatch.fnmatch((part or "").lower(), b["part"]):
            return key
    return None


def lin11(word):
    """PMBus LINEAR11: signed 5-bit exponent, signed 11-bit mantissa."""
    m = word & 0x7FF
    if m & 0x400:
        m -= 0x800
    e = (word >> 11) & 0x1F
    if e & 0x10:
        e -= 0x20
    return m * 2.0 ** e


class BridgeError(RuntimeError):
    pass


class Vrm:
    """PMBus to the VCCINT regulator, through the vrmbridge register."""

    def __init__(self, link, addr=VRM_ADDR, polls=40):
        self.l, self.addr, self.polls = link, addr, polls

    def lines(self):
        r = self.l.ask(OP_LINES)
        if (r >> 48) & 0xFFFF != BRIDGE_ID:
            raise BridgeError("the loaded bitstream is not vrmbridge "
                              f"(LINES returned 0x{r:016x})")
        return {"scl": (r >> 9) & 1, "sda": (r >> 8) & 1}

    def xfer(self, mode, reg=0, data=0, addr=None):
        a = self.addr if addr is None else addr
        arg = (mode & 7) | ((a & 0x7F) << 8) | ((reg & 0xFF) << 16) | ((data & 0xFFFF) << 24)
        self.l.exchange(OP_PMBUS, arg)
        for _ in range(self.polls):
            r = self.l.exchange(OP_NOP)
            if (r >> 48) & 0xFFFF != BRIDGE_ID:
                raise BridgeError(f"bridge stopped answering (0x{r:016x})")
            if (r >> 20) & 1:
                res = {"data": r & 0xFFFF, "addr_ack": bool(r >> 16 & 1),
                       "reg_ack": bool(r >> 17 & 1), "data_ack": bool(r >> 18 & 1),
                       "timeout": bool(r >> 19 & 1)}
                if res["timeout"]:
                    raise BridgeError("I2C clock held low by a device for >25 ms")
                return res
        raise BridgeError("PMBus transaction did not finish")

    # -- PMBus primitives: each raises unless the regulator ACKed every byte --
    def present(self):
        return self.xfer(M_QUICK)["addr_ack"]

    def _need(self, r, what, data=False):
        if not (r["addr_ack"] and r["reg_ack"] and (r["data_ack"] or not data)):
            raise BridgeError(f"regulator did not acknowledge {what}")
        return r

    def read_byte(self, reg):
        return self._need(self.xfer(M_RBYTE, reg), f"read 0x{reg:02x}")["data"] & 0xFF

    def read_word(self, reg):
        return self._need(self.xfer(M_RWORD, reg), f"read 0x{reg:02x}")["data"]

    def try_word(self, reg):
        """A word, or None if the regulator does not implement that command."""
        r = self.xfer(M_RWORD, reg)
        return r["data"] if r["addr_ack"] and r["reg_ack"] else None

    def write_byte(self, reg, v):
        self._need(self.xfer(M_WBYTE, reg, v), f"write 0x{reg:02x}", data=True)

    def write_word(self, reg, v):
        self._need(self.xfer(M_WWORD, reg, v), f"write 0x{reg:02x}", data=True)


def mv_ok(v):
    return v is not None and 300 <= v <= 1100


def survey(vrm, select_page=True):
    """Everything worth knowing before a write, read with page 0 (VCCINT)
    selected. With select_page False -- for --show -- PAGE is only read, so the
    survey writes nothing at all."""
    if select_page:
        vrm.write_byte(PAGE, 0)
    s = {"page": vrm.read_byte(PAGE),
         "revision": vrm.read_byte(PMBUS_REVISION),
         "vout_mode": vrm.read_byte(VOUT_MODE),
         "write_protect": vrm.read_byte(WRITE_PROTECT),
         "vout_command": vrm.read_word(VOUT_COMMAND),
         "read_vout": vrm.read_word(READ_VOUT),
         "status_word": vrm.read_word(STATUS_WORD)}
    for key, reg in (("vout_max", VOUT_MAX), ("ov_fault", VOUT_OV_FAULT_LIMIT),
                     ("uv_fault", VOUT_UV_FAULT_LIMIT), ("pg_on", POWER_GOOD_ON),
                     ("pg_off", POWER_GOOD_OFF)):
        s[key] = vrm.try_word(reg)
    for key, reg in (("iout_a", READ_IOUT), ("temp_c", READ_TEMPERATURE_1),
                     ("pout_w", READ_POUT)):
        w = vrm.try_word(reg)
        s[key] = lin11(w) if w is not None else None
    return s


def limits(s):
    """(floor, ceiling, reasons) once the regulator's own thresholds are in."""
    floor, ceil, why = FLOOR_MV, CEILING_MV, []
    if mv_ok(s.get("vout_max")) and s["vout_max"] - 25 < ceil:
        ceil = s["vout_max"] - 25
        why.append(f"VOUT_MAX {s['vout_max']} mV")
    if mv_ok(s.get("ov_fault")) and s["ov_fault"] - 50 < ceil:
        ceil = s["ov_fault"] - 50
        why.append(f"OV fault {s['ov_fault']} mV")
    if mv_ok(s.get("uv_fault")) and s["uv_fault"] + 75 > floor:
        floor = s["uv_fault"] + 75
        why.append(f"UV fault {s['uv_fault']} mV")
    for k, name in (("pg_on", "power-good on"), ("pg_off", "power-good off")):
        if mv_ok(s.get(k)) and s[k] + 25 > floor:
            floor = s[k] + 25
            why.append(f"{name} {s[k]} mV")
    return floor, ceil, why


def target_mv(word, board):
    if word is None:
        return None
    w = str(word).strip().lower()
    if w in ("default", "stock"):
        if not board:
            raise ValueError("board not identified, so `default` has no meaning; give mV or --board")
        return BOARDS[board]["default"]
    if w == "min":
        raise ValueError("`min` is a satellite-controller notion; give a number of mV")
    try:
        mv = int(w)
    except ValueError:
        raise ValueError(f"not a millivolt number: {word!r}")
    if not FLOOR_MV <= mv <= CEILING_MV:
        raise ValueError(f"{mv} mV is outside {FLOOR_MV}..{CEILING_MV} mV")
    return mv


def print_survey(s, sm):
    def mv(v):
        return f"{v} mV" if mv_ok(v) else ("n/a" if v is None else f"0x{v:04x}")
    print(f"regulator 0x{VRM_ADDR:02x}: PMBus rev 0x{s['revision']:02x}, VOUT_MODE "
          f"0x{s['vout_mode']:02x}, WRITE_PROTECT 0x{s['write_protect']:02x}, "
          f"STATUS_WORD 0x{s['status_word']:04x}")
    print(f"  VOUT_COMMAND {mv(s['vout_command'])}, READ_VOUT {mv(s['read_vout'])}"
          f"  (SYSMON {sm['vccint_mv']} mV)")
    print(f"  VOUT_MAX {mv(s['vout_max'])}, OV fault {mv(s['ov_fault'])}, "
          f"UV fault {mv(s['uv_fault'])}, PG on {mv(s['pg_on'])} / off {mv(s['pg_off'])}")
    extras = []
    if s["iout_a"] is not None:
        extras.append(f"IOUT {s['iout_a']:.1f} A")
    if s["pout_w"] is not None:
        extras.append(f"POUT {s['pout_w']:.1f} W")
    if s["temp_c"] is not None:
        extras.append(f"regulator {s['temp_c']:.0f} C")
    if extras:
        print("  " + ", ".join(extras))


def run(link, a, sleep=time.sleep):
    sm = link.sysmon()
    if not sm:
        print("no reply from the bridge -- is the bridge bitstream loaded?", file=sys.stderr)
        return 3
    board = a.board or board_for_part(getattr(link, "part", ""))
    print(f"device: {getattr(link, 'part', '') or '?'}, board "
          f"{BOARDS[board]['name'] if board else 'UNKNOWN'}")
    print(f"SYSMON: VCCINT {sm['vccint_mv']} mV, VCCAUX {sm['vccaux_mv']} mV, "
          f"{sm['temp_c']:.1f} C")

    vrm = Vrm(link)
    ln = vrm.lines()
    if not (ln["scl"] and ln["sda"]):
        print(f"the PMBus is not idle (SCL {ln['scl']}, SDA {ln['sda']}): something is "
              "holding it low, or this board has no pull-ups on L12/L13", file=sys.stderr)
        return 3
    if not vrm.present():
        print(f"nothing answers at 0x{VRM_ADDR:02x} on L12/L13. This is not a TH53/TH55 "
              "regulator bus, and this tool will not go looking for one elsewhere.", file=sys.stderr)
        return 3

    s = survey(vrm, select_page=not (a.show or a.vccint is None))
    print_survey(s, sm)
    if s["page"] != 0:
        print(f"  NOTE: the regulator is on PAGE {s['page']}, so these readings are not "
              "VCCINT's; a set selects PAGE 0 first", file=sys.stderr)
        if a.show or a.vccint is None:
            return 0
    floor, ceil, why = limits(s)
    print(f"  this tool allows {floor}..{ceil} mV" + (f" (from {', '.join(why)})" if why else ""))

    # Prove the encoding and the rail before anything is written.
    if not (mv_ok(s["read_vout"]) and mv_ok(s["vout_command"])
            and abs(s["read_vout"] - sm["vccint_mv"]) <= AGREE_MV
            and abs(s["vout_command"] - s["read_vout"]) <= AGREE_MV):
        print("REFUSED: the regulator's readings do not match VCCINT on the die as "
              "direct millivolts, so either this is not the VCCINT regulator or it does "
              "not use the encoding this tool assumes. Nothing was written.", file=sys.stderr)
        return 2
    print(f"  encoding confirmed: READ_VOUT {s['read_vout']} mV vs SYSMON "
          f"{sm['vccint_mv']} mV ({s['read_vout'] - sm['vccint_mv']:+d})")
    if a.show or a.vccint is None:
        return 0

    try:
        want = target_mv(a.vccint, board)
    except ValueError as e:
        print("REFUSED:", e, file=sys.stderr)
        return 2
    if not floor <= want <= ceil:
        print(f"REFUSED: {want} mV is outside {floor}..{ceil} mV for this regulator",
              file=sys.stderr)
        return 2
    if s["write_protect"] != 0:
        print(f"REFUSED: WRITE_PROTECT is 0x{s['write_protect']:02x}; this tool does "
              "not change write protection", file=sys.stderr)
        return 2
    if s["status_word"] & STATUS_FAULT_MASK:
        print(f"REFUSED: STATUS_WORD 0x{s['status_word']:04x} already reports a fault; "
              "look at it before moving the rail", file=sys.stderr)
        return 2

    start = s["vout_command"]
    cur = start
    print(f"  VCCINT {start} -> {want} mV in steps of at most {STEP_MV} mV")
    while cur != want:
        nxt = cur + max(-STEP_MV, min(STEP_MV, want - cur))
        vrm.write_word(VOUT_COMMAND, nxt)
        back = vrm.read_word(VOUT_COMMAND)
        if back != nxt:
            print(f"ABORT: wrote {nxt}, regulator holds {back}", file=sys.stderr)
            return 1
        sleep(0.05)
        st = vrm.read_word(STATUS_WORD)
        rv = vrm.read_word(READ_VOUT)
        if st & STATUS_FAULT_MASK:
            print(f"ABORT at {nxt} mV: STATUS_WORD 0x{st:04x}; returning to {start} mV",
                  file=sys.stderr)
            vrm.write_word(VOUT_COMMAND, start)
            return 1
        print(f"    {nxt} mV  (READ_VOUT {rv})")
        cur = nxt

    sleep(a.settle)
    after = link.sysmon()
    rv = vrm.read_word(READ_VOUT)
    off = after["vccint_mv"] - want
    print(f"SYSMON after: VCCINT {after['vccint_mv']} mV ({sm['vccint_mv']} before), "
          f"READ_VOUT {rv} mV")
    if abs(off) <= CONFIRM_MV:
        print(f"  CONFIRMED on-die, {off:+d} mV of the commanded value")
        return 0
    print(f"  WARNING: on-die reads {off:+d} mV from the command", file=sys.stderr)
    return 1


# --- an offline stand-in for the bridge and the regulator --------------------
class FakeLink:
    """Answers the vrmbridge register the way the RTL does, with a fake VRM."""

    def __init__(self, vout=720, part="xcvu35p", wp=0, status=0, sysmon_off=-2,
                 present=True, vout_max=950):
        self.part, self.present_ = part, present
        self.regs = {PAGE: 0, PMBUS_REVISION: 0x33, VOUT_MODE: 0x40, WRITE_PROTECT: wp,
                     VOUT_COMMAND: vout, VOUT_MAX: vout_max, VOUT_OV_FAULT_LIMIT: 977,
                     VOUT_UV_FAULT_LIMIT: 425, POWER_GOOD_ON: 500, POWER_GOOD_OFF: 450,
                     STATUS_WORD: status, READ_IOUT: 0xD280, READ_TEMPERATURE_1: 0x0024}
        self.sysmon_off, self.result, self.writes = sysmon_off, 0, []

    def sysmon(self):
        v = self.regs[VOUT_COMMAND] + self.sysmon_off
        return {"vccint_mv": v, "vccaux_mv": 1800, "temp_c": 40.0}

    def ask(self, op, arg=0):
        self.exchange(op, arg)
        return self.exchange(OP_NOP)

    def exchange(self, op, arg=0):
        prev = self.result
        if op == OP_LINES:
            self.result = (BRIDGE_ID << 48) | (1 << 9) | (1 << 8)
        elif op == OP_PMBUS:
            mode, addr, reg, data = arg & 7, (arg >> 8) & 0x7F, (arg >> 16) & 0xFF, (arg >> 24) & 0xFFFF
            ack = self.present_ and addr == VRM_ADDR
            known = reg in self.regs or reg == READ_VOUT
            d = 0
            if ack and known and mode in (M_RBYTE, M_RWORD):
                d = (self.regs[VOUT_COMMAND] - 1) if reg == READ_VOUT else self.regs[reg]
                d &= 0xFF if mode == M_RBYTE else 0xFFFF
            if ack and known and mode == M_WWORD:
                self.regs[reg] = data
                self.writes.append((reg, data))
            if ack and known and mode == M_WBYTE:
                self.regs[reg] = data & 0xFF
                self.writes.append((reg, data & 0xFF))
            self.result = ((BRIDGE_ID << 48) | (1 << 20) | (int(ack and known) << 18)
                           | (int(ack and known) << 17) | (int(ack) << 16) | d)
        return prev


def selftest():
    ns = argparse.Namespace
    fails = 0

    def case(what, link, want_rc, **kw):
        nonlocal fails
        a = ns(board=None, show=False, vccint=None, settle=0)
        vars(a).update(kw)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = run(link, a, sleep=lambda _s: None)
        ok = rc == want_rc
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what} (rc {rc})")
        if not ok:
            print("    " + buf.getvalue().replace("\n", "\n    "))
        return link

    l = case("set 800 ramps in 20 mV steps", FakeLink(), 0, vccint="800")
    steps = [d for r, d in l.writes if r == VOUT_COMMAND]   # PAGE write excluded
    ok = steps == [740, 760, 780, 800]
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} step sequence {steps}")
    l = case("show writes nothing", FakeLink(), 0, show=True)
    ok = l.writes == [] and l.regs[PAGE] == 0
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} show left the regulator untouched")
    case("ceiling refused", FakeLink(), 2, vccint="900")
    case("VOUT_MAX lowers the ceiling", FakeLink(vout_max=850), 2, vccint="840")
    case("floor refused", FakeLink(), 2, vccint="550")
    case("write-protected regulator refused", FakeLink(wp=0x80), 2, vccint="740")
    case("existing fault refused", FakeLink(status=1 << 15), 2, vccint="740")
    case("SYSMON disagreement refused", FakeLink(sysmon_off=-80), 2, vccint="740")
    case("no regulator at 0x60", FakeLink(present=False), 3, vccint="740")
    case("default on a TH55 -> 720", FakeLink(vout=760), 0, vccint="default")
    print(f"== {fails} failure(s)")
    return 1 if fails else 0


def check(a):
    """Validate a request with no hardware; changeVoltage.sh runs this first."""
    if a.vccbram is not None or a.vccmem is not None:
        print("REFUSED: only VCCINT's regulator is on the fabric bus on a TH53/TH55; "
              "--vccbram and --vccmem are C1100/U55C options", file=sys.stderr)
        return 2
    if a.vccint is None:
        print("give --vccint", file=sys.stderr)
        return 2
    try:
        mv = target_mv(a.vccint, a.board)
    except ValueError as e:
        print("REFUSED:", e, file=sys.stderr)
        return 2
    print(f"request: VCCINT -> {mv} mV (the regulator's own limits are checked on the card)")
    return 0


def describe():
    print(f"TH53 / TH55 (vrmbridge, PMBus regulator 0x{VRM_ADDR:02x} on L12/L13):")
    print(f"  VCCINT  {FLOOR_MV}..{CEILING_MV} mV, steps of {STEP_MV} mV; the regulator's "
          "VOUT_MAX, OV/UV fault and power-good limits can narrow this on the card")
    for k, b in BOARDS.items():
        print(f"  {k}: {b['name']}, default {b['default']} mV")
    print("  VCCBRAM / VCCMEM: not reachable on these boards")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set a TH53 / TH55's VCCINT over PMBus.")
    ap.add_argument("--vccint", default=None, help="core rail, mV | default")
    ap.add_argument("--vccbram", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--vccmem", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--board", default=None, choices=sorted(BOARDS),
                    help="override the board detected from the device")
    ap.add_argument("--show", action="store_true", help="read only")
    ap.add_argument("--check", action="store_true", help="validate the request; no hardware")
    ap.add_argument("--limits", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--settle", type=float, default=1.0)
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.limits:
        describe()
        return 0
    if a.check:
        return check(a)
    if a.vccbram is not None or a.vccmem is not None:
        return check(a)

    import boardguard
    import sclink
    xvc = os.environ.get("SCLINK_XVC", "").strip()
    try:
        # Before Vivado opens anything: is the XVC far end an Osprey E3xx box?
        if xvc:
            boardguard.check_xvc(xvc)
        with sclink.ScLink() as link:
            # Before any PMBus traffic: is the die a TH53/TH55's?
            boardguard.check_die(link.part, a.board)
            return run(link, a)
    except boardguard.Refused as e:
        print(boardguard.explain(e), file=sys.stderr)
        return 4
    except BridgeError as e:
        print("ERROR:", e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

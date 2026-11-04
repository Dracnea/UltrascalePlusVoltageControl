#!/usr/bin/env python3
"""Set an SQRL FK33's rails through the vrmbridge bitstream, and prove it.

    fk33set.py --show                         read everything, change nothing
    fk33set.py --vccint min --vccbram min --below-spec
                                              the lowest the board's resistors allow
    fk33set.py --vccint 800 --vcchbm default  millivolts, `min` or `default`
    fk33set.py --survey [--out f.json]        what answers on the rail bus
    fk33set.py --limits                       the models and limits, no hardware
    fk33set.py --check ...                    validate a request; no hardware
    fk33set.py --selftest                     the logic against a fake board

The bridge bitstream (rtl/vrmbridge_fk33.bit) must already be loaded --
changeVoltage.sh does that. SCLINK_FTDI=<serial> drives the FTDI directly
(host/ftdijtag.py); without it the Vivado link (host/sclink.py) is used.

HOW THE FK33 SETS ITS RAILS
    Each regulator's feedback network carries one wiper of a dual 257-step
    digital potentiometer (volatile wipers only: they come up at mid-scale,
    code 128, every power-on). On the board's I2C bus (BB24/BA24):

        VCCINT   pot 0x2C wiper 0   identified on-die (SYSMON VCCINT)
        VCCBRAM  pot 0x2E wiper 0   identified on-die (SYSMON VCCBRAM)
        VCCHBM   pot 0x2D wiper 0   by elimination: no on-die witness exists
        0x18 / 0x19 / 0x1F          temperature sensors (read only)

    A higher code is a LOWER voltage. Per rail the output follows

        V(code) = A + B / (C + code)        (mV)

    which reproduces TeamRedMiner's published FK33 conversion chart
    (doc/FPGA_FK33_MOD.txt) to its 0.1 mV rounding at all 257 codes, for both
    resistor states. A is the asymptote TRM quotes as the third figure of
    each range ("997-777 mV, 733 mV"); it is never reached.

MODDED OR STOCK
    Removing R30 lowers VCCINT's curve by 133 mV at every code; removing
    R61/R62 reshapes VCCBRAM's; swapping R84/R57 reshapes VCCHBM's. So the
    same code gives a different voltage, and tools that assume stock values
    set the wrong one on a modded board.

    This tool does not take the state on trust. It reads the wiper code and
    SYSMON's on-die voltage and names the curve the measurement lies on: on
    VCCINT the two curves are 133 mV apart everywhere, on VCCBRAM 66 mV or more
    from code 48 up (below that they meet, and the tool steps the wiper down to
    mid-scale -- the safe direction on either curve -- before it decides).
    --fk33-mod stock|modded, if given, must agree with the measurement or
    nothing is written.

    VCCHBM cannot be measured on the die, so its state cannot be detected. It
    is taken as stock unless --hbm-mod swapped says otherwise. That default is
    the safe side: at every code the swapped curve is at or below the stock
    one, so a wrong assumption can only undervolt.

SAFETY
    - Ceilings are DS923's recommended maxima (VCCINT/VCCBRAM 876 mV, VCC_HBM
      1236 mV) and are not overridable. Overvolting destroys the part.
    - Floors: a target below the lower of DS923's recommended minimum
      (VCCINT/VCCBRAM 825 mV, VCC_HBM 1164 mV) and the board's own power-on
      voltage (code 128 on its curve) is refused without --below-spec. The
      power-on term is there because the board itself ships under DS923 on
      VCCINT (stock 809 mV, modded 676 mV), and `default` must always be
      reachable. So `min` on VCCINT is refused on every board, and on VCCBRAM
      on a modded one. Below the floor the fabric is out of its specified
      range and the regulators run at the bottom of their feedback range,
      with no telemetry on this board to show how they cope. Mining runs
      there routinely; it must be asked for, not reached by default.
    - Temperatures: before anything is written, the die (SYSMON) and the
      three board sensors must read below TEMP_MAX_C, and the board sensors
      must answer at all. Both are read again at every step of a ramp; a step
      that finds the card too hot puts the rail back where it started.
    - Every move is a ladder of small code steps (at most STEP_MV each), each
      written, read back, and -- on VCCINT and VCCBRAM -- checked against
      SYSMON. A step that SYSMON does not follow puts the rail back where it
      started.
    - Moving VCCHBM must leave VCCINT, VCCBRAM and VCCAUX where they were, or
      the move is undone: that is the check that 0x2D really is not one of
      the rails the die can see.
    - Only the volatile wipers are written. Non-volatile memory, TCON and the
      temperature sensors are never written. A power cycle returns every rail
      to code 128.
    - The setting is global and survives reconfiguration: whatever is loaded
      next inherits it.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vrmset as V

M_RECV = 6

# --- the board -----------------------------------------------------------------
RAILS = ("vccint", "vccbram", "vcchbm")
POT = {"vccint": (0x2C, 0), "vccbram": (0x2E, 0), "vcchbm": (0x2D, 0)}
TEMP_SENSORS = (0x18, 0x19, 0x1F)
CODE_MAX = 256
CODE_POWER_ON = 128

# V = A + B / (C + code), mV. Fitted to TRM's FK33 chart (max error 0.05 mV).
MODEL = {
    ("vccint", "stock"):   (733.0447, 13625.264, 51.5873),
    ("vccint", "modded"):  (600.0070, 13622.152, 51.5726),
    ("vccbram", "stock"):  (772.5027, 17662.615, 102.7737),
    ("vccbram", "modded"): (600.0059, 35325.817, 102.7785),
    ("vcchbm", "stock"):   (1051.4874, 46239.109, 154.5057),
    ("vcchbm", "modded"):  (900.0017, 46234.155, 102.7871),
}
SYSMON_KEY = {"vccint": "vccint_mv", "vccbram": "vccbram_mv"}

# --- limits --------------------------------------------------------------------
CEILING_MV = {"vccint": 876, "vccbram": 876, "vcchbm": 1236}   # DS923, not overridable
FLOOR_MV = {"vccint": 825, "vccbram": 825, "vcchbm": 1164}     # DS923 minima; --below-spec passes
TEMP_MAX_C = 85.0     # die (SYSMON) and board sensors: nothing is written at or above this
STEP_MV = 10          # largest modelled change per wiper step
DETECT_MV = 30        # the measurement must sit this close to one curve ...
DETECT_GAP_MV = 50    # ... and the curves be at least this far apart where it sits
FOLLOW_MV = 8         # each step: SYSMON vs the calibrated model
CONFIRM_MV = 15       # at the end: SYSMON vs the request
QUIET_MV = 6          # VCCHBM moves: the other rails may drift this much
BRAM_DETECT_CODE = 48


def volts(rail, state, code):
    a, b, c = MODEL[(rail, state)]
    return a + b / (c + code)


def code_for(rail, state, mv):
    """The code whose modelled voltage is closest to mv, within 0..256."""
    a, b, c = MODEL[(rail, state)]
    if mv <= a:
        return CODE_MAX
    n = round(b / (mv - a) - c)
    return max(0, min(CODE_MAX, n))


def detect(rail, code, measured):
    """'stock', 'modded', or None when the two curves are too close here."""
    s, m = volts(rail, "stock", code), volts(rail, "modded", code)
    if abs(s - m) < DETECT_GAP_MV:
        return None
    ds, dm = abs(measured - s), abs(measured - m)
    if ds <= DETECT_MV and dm > DETECT_MV:
        return "stock"
    if dm <= DETECT_MV and ds > DETECT_MV:
        return "modded"
    return None


def parse_target(word, rail, state):
    """(code, wanted mV or None) for a request word, or raise ValueError."""
    if word is None:
        return None
    w = str(word).strip().lower()
    if w == "min":
        return CODE_MAX, None
    if w in ("default", "stock"):
        return CODE_POWER_ON, None
    try:
        mv = int(w)
    except ValueError:
        raise ValueError(f"{rail}: not a millivolt number, `min` or `default`: {word!r}")
    lo, hi = volts(rail, state, CODE_MAX), volts(rail, state, 0)
    if mv > CEILING_MV[rail]:
        raise ValueError(f"{rail}: {mv} mV is above the {CEILING_MV[rail]} mV ceiling")
    if not lo - 1 <= mv <= hi + 1:
        raise ValueError(f"{rail}: {mv} mV is outside what a {state} board reaches "
                         f"({lo:.0f}..{hi:.0f} mV)")
    return code_for(rail, state, mv), mv


def floor_mv(rail, state):
    """DS923's minimum, or the board's power-on voltage where the board itself ships below it."""
    return min(FLOOR_MV[rail], volts(rail, state, CODE_POWER_ON))


def floor_refusal(rail, state, mv, below_spec):
    """None, or why mv (the target) is refused: under the floor without --below-spec."""
    f = floor_mv(rail, state)
    if below_spec or mv >= f - 0.5:
        return None
    return (f"{rail}: {mv:.0f} mV is below the {f:.0f} mV floor (DS923 {FLOOR_MV[rail]} mV, or a "
            f"{state} board's power-on level if lower); give --below-spec to run the fabric out of spec")


def heat_refusal(sm, temps):
    """None, or why the card is too hot (or its board sensors silent) to touch the rails."""
    if not temps:
        return "the board temperature sensors (0x18/0x19/0x1F) do not answer"
    hot = {"die": sm.get("temp_c")} if sm.get("temp_c") is not None else {}
    hot.update(temps)
    hot = {k: v for k, v in hot.items() if v >= TEMP_MAX_C}
    if hot:
        return ("too hot: " + ", ".join(f"{k} {v:.1f} C" for k, v in hot.items())
                + f" (limit {TEMP_MAX_C:.0f} C)")
    return None


# --- the bus -------------------------------------------------------------------
class BusError(RuntimeError):
    pass


class Board:
    def __init__(self, link):
        self.l = link
        self.v = V.Vrm(link)

    def _x(self, mode, reg=0, data=0, addr=0):
        return self.v.xfer(mode, reg, data, addr=addr)

    def code(self, rail):
        addr, w = POT[rail]
        r = self._x(V.M_RWORD, (w << 4) | 0x0C, addr=addr)      # READ volatile wiper
        if not (r["addr_ack"] and r["reg_ack"]):
            raise BusError(f"pot 0x{addr:02x} did not answer a wiper read")
        d = r["data"]
        return (((d & 0xFF) << 8) | (d >> 8)) & 0x1FF           # sent MSB first

    def set_code(self, rail, code):
        addr, w = POT[rail]
        if not 0 <= code <= CODE_MAX:
            raise BusError(f"code {code} out of range")
        r = self._x(V.M_WBYTE, (w << 4) | ((code >> 8) & 1), code & 0xFF, addr=addr)
        if not (r["addr_ack"] and r["reg_ack"] and r["data_ack"]):
            raise BusError(f"pot 0x{addr:02x} did not acknowledge a wiper write")
        back = self.code(rail)
        if back != code:
            raise BusError(f"wrote code {code} to {rail}, the pot holds {back}")

    def temps(self):
        out = {}
        for a in TEMP_SENSORS:
            r = self._x(V.M_RWORD, 0x05, addr=a)
            if r["addr_ack"] and r["reg_ack"]:
                d = ((r["data"] & 0xFF) << 8) | (r["data"] >> 8)
                t = (d & 0x0FFF) / 16.0
                out[f"0x{a:02x}"] = t - 256 if d & 0x1000 else t
        return out


OP_AUXA, OP_AUXB = 0x09, 0x0A        # vrmbridge built with VRM_VAUX (FK33 builds since 2026-11-04)
BRIDGE_ID = 0x5642


def aux(link):
    """The FK33's board sensors on SYSMON VAUX, or None from a bridge without them.

    A bridge without VRM_VAUX ignores 0x09 / 0x0A and keeps its last result, so
    SYSMON (which carries no ID) is asked first: AUXB answering with the bridge
    ID is then proof of this build. Raw codes are unipolar, 1 V full scale.
    The current-sense scale is not documented by SQRL, so currents are shown
    as the sense voltage until calibrated; nothing here gates a write."""
    link.ask(V.OP_SYSMON)
    b = link.ask(OP_AUXB)
    if b is None or (b >> 48) & 0xFFFF != BRIDGE_ID or (b >> 32) & 0xFFFF:
        return None
    a = link.ask(OP_AUXA)
    code = lambda w, i: (w >> (16 * i)) & 0xFFFF
    sense = lambda c: c * 1000.0 / 65536           # mV at the pin
    return {"in12v_v": code(a, 0) * 15.2 / 65536,
            "vccint_sense_mv": sense(code(a, 1)),
            "vcchbm_sense_mv": sense(code(a, 2)),
            "vccbram_sense_mv": sense(code(a, 3)),
            "ltc3636_vccaux_c": code(b, 0) * 400.0 / 65536 - 273.15,
            "ltc3636_vccmgt_c": code(b, 1) * 400.0 / 65536 - 273.15}


def aux_line(x):
    if not x:
        return "board inputs: none (this vrmbridge_fk33.bit predates the VAUX readout)"
    return (f"board inputs: 12 V in {x['in12v_v']:.2f} V; current sense (uncalibrated) VCCINT "
            f"{x['vccint_sense_mv']:.1f} mV, VCCBRAM {x['vccbram_sense_mv']:.1f} mV, VCCHBM "
            f"{x['vcchbm_sense_mv']:.1f} mV; LTC3636 VCCAUX {x['ltc3636_vccaux_c']:.1f} C, "
            f"VCCMGT {x['ltc3636_vccmgt_c']:.1f} C")


def sysmon(link, n=6):
    s = [link.sysmon() for _ in range(n)]
    if not s or not all(s) or not s[0]["vccint_mv"]:
        return None
    avg = {k: sum(x[k] for x in s) / n for k in ("vccint_mv", "vccbram_mv", "vccaux_mv", "temp_c")
           if all(x.get(k) is not None for x in s)}
    return avg


# --- the run -------------------------------------------------------------------
def states_from(a):
    want = {}
    if a.fk33_mod:
        want = {"vccint": a.fk33_mod, "vccbram": a.fk33_mod}
    return want


def ramp(board, link, rail, state, start, goal, offset, base, log, sleep):
    """Step rail from code start to goal. Returns the last good code."""
    cur = start
    while cur != goal:
        nxt = cur
        sign = 1 if goal > cur else -1
        while nxt != goal and abs(volts(rail, state, nxt + sign) - volts(rail, state, cur)) <= STEP_MV:
            nxt += sign
        if nxt == cur:
            nxt += sign
        board.set_code(rail, nxt)
        sleep(0.15)
        sm = sysmon(link, 4)
        if sm is None:
            board.set_code(rail, start)
            raise BusError("SYSMON stopped answering mid-ramp; rail put back")
        why = heat_refusal(sm, board.temps())
        if why:
            board.set_code(rail, start)
            raise BusError(f"{rail}: {why} at code {nxt}; put back to code {start}")
        pred = volts(rail, state, nxt) + offset
        if rail in SYSMON_KEY:
            got = sm[SYSMON_KEY[rail]]
            log(f"    code {nxt:3d}  model {pred:6.1f} mV  SYSMON {got:6.1f} mV")
            if abs(got - pred) > FOLLOW_MV:
                board.set_code(rail, start)
                raise BusError(f"{rail}: SYSMON {got:.0f} mV did not follow the model "
                               f"({pred:.0f} mV) at code {nxt}; put back to code {start}")
        else:
            moved = {k: sm[k] - base[k] for k in ("vccint_mv", "vccbram_mv", "vccaux_mv")}
            log(f"    code {nxt:3d}  model {pred:6.1f} mV  (others moved "
                + ", ".join(f"{k[:-3]} {v:+.1f}" for k, v in moved.items()) + ")")
            if any(abs(v) > QUIET_MV for v in moved.values()):
                board.set_code(rail, start)
                raise BusError(f"{rail}: moving pot 0x{POT[rail][0]:02x} moved a rail the die "
                               f"can see ({moved}); it is not VCCHBM's. Put back to code {start}")
        cur = nxt
    return cur


def run(link, a, sleep=time.sleep, out=print):
    def err(msg):
        print(msg, file=sys.stderr)

    sm = sysmon(link)
    if not sm:
        err("SYSMON reads nothing -- is rtl/vrmbridge_fk33.bit loaded (and built with the "
            "scaled SYSMON clock divider)?")
        return 3
    board = Board(link)
    ln = board.v.lines()
    if not (ln["scl"] and ln["sda"]):
        err(f"the rail bus is not idle (SCL {ln['scl']}, SDA {ln['sda']}); nothing was sent")
        return 3
    out(f"device: {getattr(link, 'part', '') or '?'} (SQRL FK33)")
    out(f"SYSMON: VCCINT {sm['vccint_mv']:.0f} mV, VCCBRAM {sm['vccbram_mv']:.0f} mV, "
        f"VCCAUX {sm['vccaux_mv']:.0f} mV, die {sm['temp_c']:.1f} C")
    t = board.temps()
    if t:
        out("board sensors: " + ", ".join(f"{k} {v:.1f} C" for k, v in t.items()))
    out(aux_line(aux(link)))

    declared = states_from(a)
    hbm_state = "modded" if a.hbm_mod == "swapped" else "stock"
    codes = {r: board.code(r) for r in RAILS}
    req = {r: getattr(a, r) for r in RAILS}

    # VCCBRAM in the zone where the curves meet: step down to mid-scale first (lower on both).
    if codes["vccbram"] < BRAM_DETECT_CODE and not declared.get("vccbram") and not a.show:
        out(f"VCCBRAM code {codes['vccbram']} is where stock and modded nearly meet; stepping to "
            f"{CODE_POWER_ON} (lower on either curve) to tell them apart")
        for n in range(codes["vccbram"] + 1, CODE_POWER_ON + 1):
            board.set_code("vccbram", n)
        sleep(0.3)
        sm = sysmon(link)
        codes["vccbram"] = CODE_POWER_ON

    state, offset = {}, {}
    for r in ("vccint", "vccbram"):
        got = sm[SYSMON_KEY[r]]
        st = detect(r, codes[r], got)
        out(f"  {r.upper():7s} code {codes[r]:3d}: SYSMON {got:.0f} mV; stock curve "
            f"{volts(r, 'stock', codes[r]):.0f}, modded {volts(r, 'modded', codes[r]):.0f} "
            f"-> {st or 'UNDECIDED'}")
        if st is None and declared.get(r):
            st = declared[r]
            out(f"    taking --fk33-mod {st} for {r}: the measurement cannot decide here")
        if declared.get(r) and st != declared[r]:
            err(f"REFUSED: --fk33-mod {declared[r]}, but {r.upper()} measures as a {st} board. "
                "Nothing was written.")
            return 2
        state[r] = st
        if st:
            offset[r] = got - volts(r, st, codes[r])
    state["vcchbm"] = hbm_state
    offset["vcchbm"] = 0.0
    out(f"  VCCHBM  code {codes['vcchbm']:3d}: model {volts('vcchbm', hbm_state, codes['vcchbm']):.0f} mV "
        f"({hbm_state} -- not measurable on the die; {'declared' if a.hbm_mod else 'assumed, the safe side'})")
    if a.show or not any(req.values()):
        return 0
    why = heat_refusal(sm, t)
    if why:
        err(f"REFUSED: {why}. Nothing was written.")
        return 2

    plan = {}
    for r in RAILS:
        if req[r] is None:
            continue
        if state[r] is None:
            err(f"REFUSED: {r.upper()}'s mod state could not be measured; give --fk33-mod")
            return 2
        try:
            code, mv = parse_target(req[r], r, state[r])
        except ValueError as e:
            err(f"REFUSED: {e}")
            return 2
        want = mv if mv is not None else volts(r, state[r], code) + offset[r]
        if volts(r, state[r], code) + offset[r] > CEILING_MV[r] + 0.5:
            err(f"REFUSED: {r} at code {code} would be "
                f"{volts(r, state[r], code) + offset[r]:.0f} mV, above {CEILING_MV[r]} mV")
            return 2
        why = floor_refusal(r, state[r], want, a.below_spec)
        if why:
            err(f"REFUSED: {why}. Nothing was written.")
            return 2
        plan[r] = (code, want)
    for r, (code, want) in plan.items():
        out(f"{r.upper()}: code {codes[r]} -> {code}  (~{want:.0f} mV on a {state[r]} board)")
        base = sysmon(link)
        try:
            ramp(board, link, r, state[r], codes[r], code, offset[r], base, out, sleep)
        except BusError as e:
            err(f"ABORT: {e}")
            return 1
    sleep(a.settle)
    after = sysmon(link, 12)
    rc = 0
    out(f"SYSMON after: VCCINT {after['vccint_mv']:.0f} mV, VCCBRAM {after['vccbram_mv']:.0f} mV, "
        f"VCCAUX {after['vccaux_mv']:.0f} mV, die {after['temp_c']:.1f} C")
    t = board.temps()
    if t:
        out("board sensors after: " + ", ".join(f"{k} {v:.1f} C" for k, v in t.items()))
    for r, (code, want) in plan.items():
        if r in SYSMON_KEY:
            got = after[SYSMON_KEY[r]]
            if abs(got - want) <= CONFIRM_MV:
                out(f"  {r.upper()} CONFIRMED on-die: {got:.0f} mV ({got - want:+.0f} of {want:.0f})")
            else:
                err(f"  WARNING: {r.upper()} reads {got:.0f} mV, asked {want:.0f}")
                rc = 1
        else:
            out(f"  VCCHBM at code {board.code(r)} (~{volts(r, state[r], code):.0f} mV by the "
                f"{state[r]} curve; not measurable on the die)")
    return rc


def run_survey(link, a):
    sm = link.sysmon()
    if not sm or not sm.get("vccint_mv"):
        print("SYSMON reads nothing -- is the bridge loaded?", file=sys.stderr)
        return 3
    print(f"device: {getattr(link, 'part', '') or '?'}")
    print(f"SYSMON: VCCINT {sm['vccint_mv']} mV, VCCBRAM {sm['vccbram_mv']} mV, "
          f"VCCAUX {sm['vccaux_mv']} mV, {sm['temp_c']:.1f} C")
    vrm = V.Vrm(link)
    ln = vrm.lines()
    print(f"bus: SCL {ln['scl']}, SDA {ln['sda']}")
    if not (ln["scl"] and ln["sda"]):
        print("the bus is not idle; nothing was sent", file=sys.stderr)
        return 3
    found = {}
    for addr in range(0x08, 0x78):                    # RECEIVE_BYTE: no command byte is sent
        r = vrm.xfer(M_RECV, 0, 0, addr=addr)
        if r["addr_ack"]:
            found[addr] = r["data"] & 0xFF
    for addr, b in found.items():
        print(f"  0x{addr:02x} answers (receive byte 0x{b:02x})")
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"part": getattr(link, "part", ""), "sysmon": sm,
                       "addresses": {f"0x{k:02x}": v for k, v in found.items()}}, f, indent=2)
        print(f"wrote {a.out}")
    return 0


# --- an offline FK33 -------------------------------------------------------------
class FakeLink:
    """The vrmbridge register over a fake FK33 bus: three pots, three sensors."""

    def __init__(self, modded=False, hbm_swapped=False, codes=None, offset=3.0,
                 hbm_moves_int=False, lag=False, board_c=40.0, no_sensors=False, heat_after=None):
        self.part = "xcvu33p"
        self.codes = {(a, w): CODE_POWER_ON for a in (0x2C, 0x2D, 0x2E) for w in (0, 1)}
        for r, c in (codes or {}).items():
            self.codes[POT[r]] = c
        self.st = {"vccint": "modded" if modded else "stock", "vccbram": "modded" if modded else "stock",
                   "vcchbm": "modded" if hbm_swapped else "stock"}
        self.offset, self.hbm_moves_int, self.lag = offset, hbm_moves_int, lag
        self.board_c, self.no_sensors, self.heat_after = board_c, no_sensors, heat_after
        self.result, self.writes = 0, []

    def _v(self, rail):
        return volts(rail, self.st[rail], self.codes[POT[rail]]) + self.offset

    def sysmon(self):
        vi = self._v("vccint") + (0.4 * (self.codes[POT["vcchbm"]] - 128) if self.hbm_moves_int else 0)
        if self.lag:
            vi = volts("vccint", self.st["vccint"], CODE_POWER_ON) + self.offset
        return {"vccint_mv": vi, "vccbram_mv": self._v("vccbram"), "vccaux_mv": 1783.0, "temp_c": 37.0}

    def ask(self, op, arg=0):
        self.exchange(op, arg)
        return self.exchange(V.OP_NOP)

    def exchange(self, op, arg=0):
        prev = self.result
        if op == V.OP_LINES:
            self.result = (V.BRIDGE_ID << 48) | (1 << 9) | (1 << 8)
        elif op == V.OP_PMBUS:
            mode, addr, reg, data = arg & 7, (arg >> 8) & 0x7F, (arg >> 16) & 0xFF, (arg >> 24) & 0xFFFF
            ack, rack, d = False, False, 0
            if addr in (0x2C, 0x2D, 0x2E):
                ack = True
                w, cmd = reg >> 4, (reg >> 2) & 3
                if w in (0, 1) and mode == V.M_RWORD and cmd == 3:
                    rack = True
                    c = self.codes[(addr, w)]
                    d = ((c & 0xFF) << 8) | (c >> 8)
                elif w in (0, 1) and mode == V.M_WBYTE and cmd == 0:
                    rack = True
                    self.codes[(addr, w)] = ((reg & 1) << 8) | (data & 0xFF)
                    self.writes.append((addr, w, self.codes[(addr, w)]))
            elif addr in TEMP_SENSORS and not self.no_sensors:
                ack = rack = mode == V.M_RWORD
                c = self.board_c
                if self.heat_after is not None and len(self.writes) >= self.heat_after:
                    c = TEMP_MAX_C + 5
                raw = int(round(c * 16)) & 0x0FFF
                d = ((raw & 0xFF) << 8) | (raw >> 8) if reg == 0x05 else 0
            self.result = ((V.BRIDGE_ID << 48) | (1 << 20) | (int(rack) << 18) | (int(rack) << 17)
                           | (int(ack) << 16) | d)
        return prev


def selftest():
    import contextlib
    import io
    fails = 0

    def case(what, link, want_rc, **kw):
        nonlocal fails
        a = argparse.Namespace(fk33_mod=None, hbm_mod=None, show=False, settle=0,
                               vccint=None, vccbram=None, vcchbm=None, below_spec=False)
        vars(a).update(kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = run(link, a, sleep=lambda _s: None, out=print)
        ok = rc == want_rc
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what} (rc {rc})")
        if not ok:
            print("    " + buf.getvalue().replace("\n", "\n    "))
        return link, buf.getvalue()

    # the model against TRM's own ranges
    for (r, s), lo, hi in ((("vccint", "stock"), 777.3, 997.2), (("vccint", "modded"), 644.3, 864.1),
                           (("vccbram", "stock"), 821.7, 944.4), (("vccbram", "modded"), 698.5, 943.7),
                           (("vcchbm", "stock"), 1164.1, 1350.8), (("vcchbm", "modded"), 1028.9, 1349.8)):
        ok = abs(volts(r, s, 256) - lo) < 0.15 and abs(volts(r, s, 0) - hi) < 0.15
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} model {r} {s}: {volts(r, s, 256):.1f}..{volts(r, s, 0):.1f} mV")
    ok = all(volts("vcchbm", "modded", n) <= volts("vcchbm", "stock", n) + 0.5 for n in range(257))
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} swapped VCCHBM never above stock at the same code")

    l, _ = case("stock board, show writes nothing", FakeLink(), 0, show=True)
    fails += bool(l.writes)
    l, o = case("stock board: vccint/vccbram/vcchbm min", FakeLink(), 0, vccint="min", vccbram="min", vcchbm="min",
                below_spec=True)
    ok = all(l.codes[POT[r]] == 256 for r in RAILS) and "CONFIRMED" in o
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} all three at code 256, confirmed on-die")
    case("modded board detected and set to 650", FakeLink(modded=True), 0, vccint="650", below_spec=True)
    case("--fk33-mod stock on a modded board refused", FakeLink(modded=True), 2, fk33_mod="stock", vccint="min")
    case("--fk33-mod modded on a stock board refused", FakeLink(), 2, fk33_mod="modded", vccint="min")
    case("stock board refuses 700 mV (below its reach)", FakeLink(), 2, vccint="700")
    case("ceiling refused (900 on a stock board)", FakeLink(), 2, vccint="900")
    l, _ = case("low BRAM code steps to 128 to detect", FakeLink(codes={"vccbram": 10}), 0, vccbram="min",
                below_spec=True)
    fails += l.codes[POT["vccbram"]] != 256
    l, _ = case("SYSMON not following -> abort and restore", FakeLink(lag=True), 1, vccint="min", below_spec=True)
    ok = l.codes[POT["vccint"]] == CODE_POWER_ON
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} VCCINT put back to code 128")
    l, _ = case("HBM pot that moves VCCINT -> abort and restore", FakeLink(hbm_moves_int=True), 1, vcchbm="min")
    ok = l.codes[POT["vcchbm"]] == CODE_POWER_ON
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} VCCHBM put back to code 128")
    l, _ = case("default returns to code 128", FakeLink(codes={"vccint": 256}), 0, vccint="default")
    fails += l.codes[POT["vccint"]] != 128
    # floors
    l, _ = case("min below DS923 floor refused without --below-spec", FakeLink(), 2, vccint="min")
    fails += bool(l.writes)
    l, _ = case("modded VCCBRAM min (~701 mV) refused without --below-spec", FakeLink(modded=True), 2,
                vccbram="min")
    fails += bool(l.writes)
    case("stock VCCBRAM min (~825 mV, at DS923's minimum) allowed", FakeLink(), 0, vccbram="min")
    case("800 mV VCCINT refused on a stock board (power-on 809)", FakeLink(), 2, vccint="800")
    case("815 mV VCCINT allowed on a stock board", FakeLink(), 0, vccint="815")
    case("modded VCCINT 700 allowed (its power-on is 676)", FakeLink(modded=True), 0, vccint="700")
    case("modded VCCINT 650 refused without --below-spec", FakeLink(modded=True), 2, vccint="650")
    case("VCCHBM min on a stock board (1164 mV) is in spec", FakeLink(), 0, vcchbm="min")
    case("VCCHBM min on a swapped board (~1029 mV) refused", FakeLink(hbm_swapped=True), 2, vcchbm="min",
         hbm_mod="swapped")
    # temperatures
    l, _ = case("hot board refused before any write", FakeLink(board_c=TEMP_MAX_C + 1), 2, vccint="830")
    fails += bool(l.writes)
    l, _ = case("silent board sensors refused", FakeLink(no_sensors=True), 2, vccint="830")
    fails += bool(l.writes)
    case("hot board still allows --show", FakeLink(board_c=TEMP_MAX_C + 1), 0, show=True)
    l, _ = case("card heats mid-ramp -> abort and restore", FakeLink(heat_after=2), 1, vccint="min",
                below_spec=True)
    ok = l.codes[POT["vccint"]] == CODE_POWER_ON
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} VCCINT put back to code 128 after the heat abort")

    # VAUX readout: a VRM_VAUX bridge answers AUXB with its ID; an older one keeps the SYSMON word.
    class AuxLink:
        def __init__(self, vaux):
            self.vaux, self.result = vaux, 0

        def ask(self, op, arg=0):
            if op == V.OP_SYSMON:
                self.result = (0x4600 << 48) | (0x9900 << 32) | (0x4500 << 16) | 0x9C00
            elif self.vaux and op == OP_AUXB:
                self.result = (BRIDGE_ID << 48) | (0x4B00 << 16) | 0x4A00
            elif self.vaux and op == OP_AUXA:
                self.result = (0x0800 << 48) | (0x0400 << 32) | (0x2000 << 16) | 0xCA00
            return self.result
    x = aux(AuxLink(True))
    ok = (x is not None and abs(x["in12v_v"] - 0xCA00 * 15.2 / 65536) < 1e-9
          and abs(x["vccint_sense_mv"] - 125.0) < 1e-9 and abs(x["vccbram_sense_mv"] - 31.25) < 1e-9
          and abs(x["vcchbm_sense_mv"] - 15.625) < 1e-9
          and abs(x["ltc3636_vccaux_c"] - (0x4A00 * 400 / 65536 - 273.15)) < 1e-9)
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} VAUX bridge: 12 V, three current senses, LTC3636 temps decoded")
    ok = aux(AuxLink(False)) is None and "predates" in aux_line(None)
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} a bridge without VAUX is told apart (no readout, said so)")
    print(f"== {fails} failure(s)")
    return 1 if fails else 0


def describe():
    print("SQRL FK33 (vrmbridge on the PMIC I2C, BB24/BA24): digital pots, code 0..256, higher = lower")
    for r in RAILS:
        addr, w = POT[r]
        print(f"  {r.upper():7s} pot 0x{addr:02x} wiper {w}, ceiling {CEILING_MV[r]} mV, "
              f"floor {FLOOR_MV[r]} mV or the board's power-on level if lower (--below-spec passes it)")
        for s in ("stock", "modded"):
            print(f"    {s:6s} {volts(r, s, 256):6.1f} (min) .. {volts(r, s, CODE_POWER_ON):6.1f} (power-on, "
                  f"default) .. {volts(r, s, 0):6.1f} mV (code 0)")
    print("  mod state: measured on VCCINT/VCCBRAM; VCCHBM assumed stock unless --hbm-mod swapped")
    print(f"  temperature: nothing is written with the die or a board sensor at {TEMP_MAX_C:.0f} C or above")


def check(a):
    if a.survey or a.show:
        if any(getattr(a, r) is not None for r in RAILS):
            print("--show / --survey read only; drop the rails", file=sys.stderr)
            return 2
        print("request: read only, nothing will be written")
        return 0
    if a.fk33_mod not in (None, "stock", "modded"):
        print("REFUSED: --fk33-mod is stock or modded", file=sys.stderr)
        return 2
    if a.hbm_mod not in (None, "stock", "swapped"):
        print("REFUSED: --hbm-mod is stock or swapped", file=sys.stderr)
        return 2
    if not any(getattr(a, r) is not None for r in RAILS):
        print("give --vccint / --vccbram / --vcchbm (or --vccmem), or --show", file=sys.stderr)
        return 2
    for r in RAILS:
        w = getattr(a, r)
        if w is None:
            continue
        # The state is measured on the card; check the word against both curves here.
        oks = []
        for s in ("stock", "modded"):
            try:
                code, mv = parse_target(w, r, s)
                # Offline there is no measured offset (a few mV, calibrated on the card): give a
                # modelled target FOLLOW_MV of slack here; run() judges it exactly.
                why = floor_refusal(r, s, mv if mv is not None else volts(r, s, code) + FOLLOW_MV,
                                    a.below_spec)
                if why:
                    raise ValueError(why)
                oks.append(s)
            except ValueError as e:
                last = e
        if not oks:
            print(f"REFUSED: {last}", file=sys.stderr)
            return 2
    print("request: " + ", ".join(f"{r.upper()} -> {getattr(a, r)}" for r in RAILS
                                   if getattr(a, r) is not None)
          + " (the mod state is measured on the card before anything is written)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set an SQRL FK33's rails.")
    ap.add_argument("--vccint", default=None, help="mV | min | default")
    ap.add_argument("--vccbram", default=None, help="mV | min | default")
    ap.add_argument("--vcchbm", "--vccmem", dest="vcchbm", default=None, help="mV | min | default")
    ap.add_argument("--fk33-mod", default=None, help="stock | modded: cross-check the measurement")
    ap.add_argument("--hbm-mod", default=None, help="stock | swapped (R84/R57); default stock")
    ap.add_argument("--below-spec", action="store_true",
                    help="allow targets under DS923's minima (VCCINT/VCCBRAM 825, VCC_HBM 1164 mV)")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--limits", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--settle", type=float, default=1.0)
    a, _rest = ap.parse_known_args(argv)
    if a.selftest:
        return selftest()
    if a.limits:
        describe()
        return 0
    if a.check:
        return check(a)
    if check(a):
        return 2
    import ftdijtag
    try:
        with ftdijtag.link() as link:
            if a.survey:
                return run_survey(link, a)
            return run(link, a)
    except (BusError, V.BridgeError, ftdijtag.FtdiError) as e:
        print("ERROR:", e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

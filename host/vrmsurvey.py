#!/usr/bin/env python3
"""Discover what else a TH53 / TH55's PMBus can reach -- read-only.

    vrmset.py --survey [--out survey.json]      (through changeVoltage.sh)

VCCINT is the only rail this tool sets on these boards. Whether any other rail
can be set depends on what is on the bus, and nobody has looked past VCCINT
yet. This survey looks, and changes nothing a design or the board depends on:

  1. Scan every 7-bit address with RECEIVE_BYTE, which sends no command byte.
     0x0C answering is the SMBus Alert Response Address: a device on the bus
     has SMBALERT# asserted, and reading it returns that device's address.
  2. Read the regulator's identity: MFR_ID, MFR_MODEL, MFR_REVISION,
     IC_DEVICE_ID, IC_DEVICE_REV (block reads), PMBus revision, CAPABILITY.
  3. Walk PAGE 0..3. For each page the regulator accepts, read its output
     setpoint, measured output, limits, current, temperature and status.
     PAGE is the one register written; it is put back to 0 before the survey
     returns, which is also its power-on value.
  4. Name each page by matching its measured output against SYSMON's on-die
     VCCINT, VCCBRAM and VCCAUX. A page that matches none of them is reported
     with the rails its voltage could be, and is NOT treated as identified.

Only a page identified by step 4 could ever be offered for setting, and only
with that rail's limits (docs/th5x.md, "Other rails").
"""
import json
import sys

import vrmset as V

M_RECV, M_BLOCK = 6, 7

ARA_ADDR = 0x0C
OPERATION, ON_OFF_CONFIG, CAPABILITY = 0x01, 0x02, 0x19
MFR_ID, MFR_MODEL, MFR_REVISION = 0x99, 0x9A, 0x9B
IC_DEVICE_ID, IC_DEVICE_REV = 0xAD, 0xAE
PAGES = range(4)
MATCH_MV = 25

# Rails an UltraScale+ HBM part (VU33P / VU35P) is powered from, with their
# nominal voltage, for naming a page SYSMON cannot see. A candidate, not an
# identification.
NOMINAL_MV = (
    ("VCCINT", 850), ("VCCINT (0.72 V parts)", 720), ("VCCBRAM / VCCINT_IO", 850),
    ("MGTAVCC", 900), ("VCC_HBM", 1200), ("MGTAVTT", 1200), ("VCCAUX / VCCAUX_IO", 1800),
    ("MGTVCCAUX", 1800), ("VCCO (1.8 V banks)", 1800), ("VCCAUX_HBM", 2500),
)


def block(vrm, reg, limit=32):
    """A PMBus block read, one byte per transaction. None if not implemented."""
    r = vrm.xfer(M_BLOCK, reg, 0)
    if not (r["addr_ack"] and r["reg_ack"]):
        return None
    n = (r["data"] >> 8) & 0xFF
    if n == 0 or n == 0xFF:
        return b""
    out = bytearray([r["data"] & 0xFF])
    for k in range(1, min(n, limit)):
        out.append(vrm.xfer(M_BLOCK, reg, k)["data"] & 0xFF)
    return bytes(out)


def text(b):
    if b is None:
        return None
    s = b.rstrip(b"\x00").decode("ascii", "replace")
    return s if s.isprintable() else b.hex()


def try_byte(vrm, reg):
    r = vrm.xfer(V.M_RBYTE, reg)
    return r["data"] & 0xFF if r["addr_ack"] and r["reg_ack"] else None


def linear16(raw, vout_mode):
    """VOUT in mV under PMBus LINEAR16, if VOUT_MODE says linear; else None."""
    if raw is None or vout_mode is None or (vout_mode >> 5) != 0:
        return None
    e = vout_mode & 0x1F
    if e & 0x10:
        e -= 0x20
    return round(raw * 2.0 ** e * 1000)


def as_mv(raw, vout_mode):
    """Both readings of a VOUT word: plain millivolts (what page 0 uses on these
    boards) and LINEAR16. Whichever SYSMON corroborates is the real one."""
    return {"raw": raw, "direct_mv": raw, "linear16_mv": linear16(raw, vout_mode)}


def scan(vrm):
    found = []
    for a in range(0x08, 0x78):
        r = vrm.xfer(M_RECV, 0, 0, addr=a)
        if r["addr_ack"]:
            found.append(a)
    return found


def name_page(pg, sm):
    """(identified rail or None, candidates) for one page's measured output."""
    refs = {"VCCINT": sm.get("vccint_mv"), "VCCBRAM": sm.get("vccbram_mv"),
            "VCCAUX": sm.get("vccaux_mv")}
    for reading in (pg["read_vout"]["direct_mv"], pg["read_vout"]["linear16_mv"]):
        if reading is None:
            continue
        hits = [k for k, v in refs.items() if v and abs(reading - v) <= MATCH_MV]
        if len(hits) == 1:
            return hits[0], []
    v = pg["read_vout"]["direct_mv"]
    cands = [n for n, nom in NOMINAL_MV if v and abs(v - nom) <= 100]
    return None, cands


def survey(link, vrm):
    sm = link.sysmon()
    rep = {"part": getattr(link, "part", ""), "sysmon": sm, "bus": {}, "regulator": {},
           "pages": {}}
    rep["bus"]["addresses"] = [f"0x{a:02x}" for a in scan(vrm)]
    if ARA_ADDR in [int(a, 16) for a in rep["bus"]["addresses"]]:
        r = vrm.xfer(M_RECV, 0, 0, addr=ARA_ADDR)
        rep["bus"]["alert_response"] = f"0x{(r['data'] & 0xFF) >> 1:02x}"
    if not vrm.present():
        rep["regulator"] = None
        return rep

    reg = rep["regulator"]
    reg["address"] = f"0x{vrm.addr:02x}"
    for key, c in (("mfr_id", MFR_ID), ("mfr_model", MFR_MODEL),
                   ("mfr_revision", MFR_REVISION), ("ic_device_id", IC_DEVICE_ID),
                   ("ic_device_rev", IC_DEVICE_REV)):
        b = block(vrm, c)
        reg[key] = text(b)
        reg[key + "_hex"] = b.hex() if b else None
    reg["pmbus_revision"] = try_byte(vrm, V.PMBUS_REVISION)
    reg["capability"] = try_byte(vrm, CAPABILITY)

    try:
        for p in PAGES:
            vrm.write_byte(V.PAGE, p)
            if try_byte(vrm, V.PAGE) != p:
                break
            mode = try_byte(vrm, V.VOUT_MODE)
            pg = {"vout_mode": mode,
                  "operation": try_byte(vrm, OPERATION),
                  "on_off_config": try_byte(vrm, ON_OFF_CONFIG),
                  "status_word": vrm.try_word(V.STATUS_WORD)}
            for key, c in (("vout_command", V.VOUT_COMMAND), ("read_vout", V.READ_VOUT),
                           ("vout_max", V.VOUT_MAX), ("ov_fault", V.VOUT_OV_FAULT_LIMIT),
                           ("uv_fault", V.VOUT_UV_FAULT_LIMIT), ("pg_on", V.POWER_GOOD_ON),
                           ("pg_off", V.POWER_GOOD_OFF)):
                pg[key] = as_mv(vrm.try_word(c), mode)
            for key, c in (("iout_a", V.READ_IOUT), ("temp_c", V.READ_TEMPERATURE_1),
                           ("pout_w", V.READ_POUT)):
                w = vrm.try_word(c)
                pg[key] = round(V.lin11(w), 2) if w is not None else None
            pg["identified_as"], pg["candidates"] = name_page(pg, sm)
            rep["pages"][str(p)] = pg
    finally:
        vrm.write_byte(V.PAGE, 0)
    return rep


def print_report(rep):
    sm = rep["sysmon"]
    print(f"part {rep['part'] or '?'}; SYSMON VCCINT {sm['vccint_mv']} mV, VCCBRAM "
          f"{sm.get('vccbram_mv')} mV, VCCAUX {sm['vccaux_mv']} mV")
    print(f"bus L12/L13: {', '.join(rep['bus']['addresses']) or 'nothing answers'}")
    if "alert_response" in rep["bus"]:
        print(f"  0x0c is the SMBus Alert Response Address: {rep['bus']['alert_response']} "
              "has SMBALERT# asserted")
    reg = rep["regulator"]
    if not reg:
        print("no regulator at 0x60")
        return
    print(f"regulator {reg['address']}: MFR_ID {reg['mfr_id']!r}, MFR_MODEL "
          f"{reg['mfr_model']!r}, MFR_REVISION {reg['mfr_revision']!r}")
    print(f"  IC_DEVICE_ID {reg['ic_device_id_hex']}, IC_DEVICE_REV {reg['ic_device_rev_hex']}, "
          f"PMBus rev {reg['pmbus_revision']}, CAPABILITY {reg['capability']}")
    for p, pg in rep["pages"].items():
        rv, vc = pg["read_vout"], pg["vout_command"]
        who = pg["identified_as"] or ("unidentified; could be " + ", ".join(pg["candidates"])
                                      if pg["candidates"] else "unidentified")
        mode = pg["vout_mode"]
        print(f"  PAGE {p}: {who}")
        print(f"    VOUT_MODE {mode if mode is None else f'0x{mode:02x}'}, VOUT_COMMAND raw "
              f"{vc['raw']} (LINEAR16 {vc['linear16_mv']} mV), READ_VOUT raw {rv['raw']} "
              f"(LINEAR16 {rv['linear16_mv']} mV)")
        print(f"    VOUT_MAX {pg['vout_max']['raw']}, OV {pg['ov_fault']['raw']}, UV "
              f"{pg['uv_fault']['raw']}, PG on/off {pg['pg_on']['raw']}/{pg['pg_off']['raw']}, "
              f"IOUT {pg['iout_a']} A, {pg['temp_c']} C, STATUS_WORD {pg['status_word']}, "
              f"OPERATION {pg['operation']}")
    print("PAGE restored to 0.")


def run(link, a):
    sm = link.sysmon()
    if not sm:
        print("no reply from the bridge -- is the bridge bitstream loaded?", file=sys.stderr)
        return 3
    vrm = V.Vrm(link)
    ln = vrm.lines()
    if not (ln["scl"] and ln["sda"]):
        print(f"the PMBus is not idle (SCL {ln['scl']}, SDA {ln['sda']})", file=sys.stderr)
        return 3
    rep = survey(link, vrm)
    print_report(rep)
    if getattr(a, "out", None):
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=1)
        print(f"written to {a.out} -- this file is what to send back")
    return 0


# --- offline check against a fake two-page regulator --------------------------
class FakeTwoPage(V.FakeLink):
    def __init__(self):
        super().__init__(vout=720)
        self.page1 = {V.VOUT_COMMAND: 850, V.READ_VOUT: 849, V.VOUT_MODE: 0x40}
        self.blocks = {MFR_MODEL: b"TESTPART", MFR_ID: b"IFX"}

    def sysmon(self):
        d = super().sysmon()
        d["vccbram_mv"] = 851
        return d

    def exchange(self, op, arg=0):
        if op != V.OP_PMBUS:
            return super().exchange(op, arg)
        mode, addr = arg & 7, (arg >> 8) & 0x7F
        reg, data = (arg >> 16) & 0xFF, (arg >> 24) & 0xFFFF
        prev = self.result
        hdr = (V.BRIDGE_ID << 48) | (1 << 20)
        if mode == M_RECV:
            ack = addr in (V.VRM_ADDR, ARA_ADDR)
            d = (V.VRM_ADDR << 1) if addr == ARA_ADDR else 0x5A
            self.result = hdr | (int(ack) << 16) | (d if ack else 0)
            return prev
        if mode == M_BLOCK:
            b = self.blocks.get(reg)
            ok = addr == V.VRM_ADDR and b is not None
            k = data & 0xFF
            d = ((len(b) << 8) | (b[k] if k < len(b) else 0xFF)) if ok else 0
            self.result = hdr | (int(ok) << 17) | (int(addr == V.VRM_ADDR) << 16) | d
            return prev
        if mode == V.M_WBYTE and reg == V.PAGE and addr == V.VRM_ADDR:
            self.regs[V.PAGE] = data & 0xFF if (data & 0xFF) in (0, 1) else self.regs[V.PAGE]
            self.writes.append((reg, data & 0xFF))
            self.result = hdr | (7 << 16)
            return prev
        if self.regs[V.PAGE] == 1 and reg in self.page1 and mode in (V.M_RBYTE, V.M_RWORD):
            self.result = hdr | (7 << 16) | self.page1[reg]
            return prev
        return super().exchange(op, arg)


def selftest():
    import contextlib
    import io
    link = FakeTwoPage()
    vrm = V.Vrm(link)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = survey(link, vrm)
    fails = 0

    def check(what, ok):
        nonlocal fails
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}")

    check(f"bus scan finds 0x0c and 0x60: {rep['bus']['addresses']}",
          rep["bus"]["addresses"] == ["0x0c", "0x60"])
    check(f"0x0c read as the alert response for {rep['bus'].get('alert_response')}",
          rep["bus"].get("alert_response") == "0x60")
    check(f"MFR_MODEL block read = {rep['regulator']['mfr_model']!r}",
          rep["regulator"]["mfr_model"] == "TESTPART")
    check(f"pages found: {list(rep['pages'])}", list(rep["pages"]) == ["0", "1"])
    check(f"page 0 identified as {rep['pages']['0']['identified_as']}",
          rep["pages"]["0"]["identified_as"] == "VCCINT")
    check(f"page 1 identified as {rep['pages']['1']['identified_as']}",
          rep["pages"]["1"]["identified_as"] == "VCCBRAM")
    check("only PAGE was written, and it ends at 0",
          all(r == V.PAGE for r, _ in link.writes) and link.regs[V.PAGE] == 0)
    print(f"== {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())

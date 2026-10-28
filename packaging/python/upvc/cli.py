"""upvc -- set the rails of an FPGA card over an FTDI cable, without Vivado.

    upvc list [--probe]
    upvc show --serial S
    upvc set  --serial S --vccint <mV|min|default> [--vccbram ..] [--vccmem ..]
    upvc limits | selftest | bits

This is the FTDI path of changeVoltage.sh, step for step: read the IDCODE, pick
the family from the part, run the board guard and the request check (nothing is
loaded if either refuses), check the bridge's SHA256 against SHA256SUMS, unpack
it to a temporary directory, load it, then run the setter. It never picks a
cable for you: --serial is always required for anything that touches a card.

OVERVOLTING DESTROYS THE PART. The ceilings live in the setters and cannot be
overridden. Undervolting makes the fabric go mute until it is reprogrammed.
"""
import argparse
import hashlib
import lzma
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import __version__, _paths

HOST, PREBUILT, BOARDS = _paths.locate()
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VID = "0403"
PIDS = ("6010", "6011")            # FT2232H, FT4232H

# part (lower case) substring -> (family, prebuilt bridge). Same order and same
# patterns as the Tcl `switch -glob` in changeVoltage.sh.
BRIDGES = (
    ("u55c", "sc", "scbridge_u55c.bit.xz"),
    ("u55n", "sc", "scbridge_c1100.bit.xz"),
    ("vu33p_civ", "vrm", "vrmbridge_th53.bit.xz"),
    ("vu33p", "fk33", "vrmbridge_fk33.bit.xz"),
    ("vu35p", "vrm", "vrmbridge_th55.bit.xz"),
)


class Die(Exception):
    def __init__(self, msg, code=1):
        Exception.__init__(self, msg)
        self.code = code


def die(msg, code=1):
    raise Die(msg, code)


# ---- running the host scripts --------------------------------------------------
def _env(serial=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_PARENT + os.pathsep + env.get("PYTHONPATH", "")
    if serial:
        env["SCLINK_FTDI"] = serial
    return env


def _cmd(script, args):
    return [sys.executable, "-m", "upvc._shim", os.path.join(HOST, script)] + list(args)


def run(script, args, serial=None, capture=False):
    sys.stdout.flush()
    p = subprocess.run(_cmd(script, args), env=_env(serial), universal_newlines=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None)
    return p.returncode, (p.stdout if capture else "")


# ---- USB enumeration (no device is opened) --------------------------------------
def _sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_cables(root="/sys/bus/usb/devices"):
    """FTDI 0403:6010 / 0403:6011 devices, from sysfs. Opens nothing."""
    out = []
    if not os.path.isdir(root):
        return out
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if _sysfs(os.path.join(p, "idVendor")).lower() != VID:
            continue
        pid = _sysfs(os.path.join(p, "idProduct")).lower()
        if pid not in PIDS:
            continue
        out.append({"serial": _sysfs(os.path.join(p, "serial")),
                    "product": _sysfs(os.path.join(p, "product")),
                    "pid": pid, "bus": _sysfs(os.path.join(p, "busnum")),
                    "dev": _sysfs(os.path.join(p, "devnum"))})
    return out


def probe(serial):
    """Read the IDCODE through ftdijtag, opening interface A. Returns a string."""
    code, out = run("ftdijtag.py", ["--serial", serial, "--idcode"], capture=True)
    out = out.strip().splitlines()
    return out[-1] if out else "(no answer)"


# ---- checksums and bridges -----------------------------------------------------
def read_sums(path):
    sums = {}
    with open(path) as f:
        for line in f:
            m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$", line)
            if m:
                sums[m.group(2)] = m.group(1).lower()
    return sums


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verified_bridge(name):
    """Path of a prebuilt .xz whose SHA256 matches SHA256SUMS, or Die."""
    xz = os.path.join(PREBUILT, name)
    if not os.path.isfile(xz):
        die("bridge not found: %s" % xz)
    sums_path = os.path.join(PREBUILT, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        die("SHA256SUMS missing next to the bridges: %s" % sums_path)
    want = read_sums(sums_path).get(name)
    if not want:
        die("%s has no entry in SHA256SUMS; refusing to load it" % name)
    got = sha256(xz)
    if got != want:
        die("SHA256 mismatch for %s (got %s, SHA256SUMS says %s); refusing to load it"
            % (name, got, want))
    return xz


def unpack(xz, tmpdir):
    raw = os.path.join(tmpdir, os.path.basename(xz)[:-3])
    with lzma.open(xz, "rb") as src, open(raw, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return raw


def family_for(part):
    p = (part or "").lower()
    for key, fam, bridge in BRIDGES:
        if key in p:
            return fam, bridge
    return None, None


# ---- the same host checks as changeVoltage.sh ------------------------------------
def pci_still_present():
    """changeVoltage.sh step 2: a PCIe-visible card must be removed before JTAG."""
    pci = os.environ.get("SCVOLT_PCI", "")
    if not pci and shutil.which("lspci"):
        try:
            out = subprocess.run(["lspci", "-Dn", "-d", "10ee:"], universal_newlines=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
            pci = out.split()[0] if out.split() else ""
        except OSError:
            pci = ""
    if pci and os.path.isdir("/sys/bus/pci/devices/" + pci):
        return pci
    return None


def jtag_holders():
    """Other JTAG tools running by process name (pgrep -x), as in changeVoltage.sh."""
    if not shutil.which("pgrep"):
        return []
    found = []
    for n in ("changeVoltage", "openFPGALoader", "xsdb"):
        r = subprocess.run(["pgrep", "-x", n], universal_newlines=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pids = [p for p in r.stdout.split() if p != str(os.getpid())]
        if pids:
            found.append("%s(%s)" % (n, ",".join(pids)))
    return found


def has_rail_or_action(rest):
    acts = ("--vccint", "--vccbram", "--vccmem", "--vcchbm", "--show", "--survey",
            "--enable-menu")
    return any(t.split("=")[0] in acts for t in rest)


# ---- commands -----------------------------------------------------------------
def cmd_list(a):
    cables = find_cables()
    if not cables:
        if not os.path.isdir("/sys/bus/usb/devices"):
            print("upvc list reads Linux sysfs; on this OS find the serial with your USB "
                  "tool and pass it as --serial.")
        else:
            print("no FTDI 0403:6010 / 0403:6011 device found")
        return 0
    for c in cables:
        line = "%s  %s:%s  %s  (usb %s/%s)" % (c["serial"] or "(no serial)", VID, c["pid"],
                                                c["product"], c["bus"], c["dev"])
        if a.probe and c["serial"]:
            line += "\n    " + probe(c["serial"])
        print(line)
    if not a.probe:
        print("(add --probe to read each cable's IDCODE; that opens channel A, so it "
              "fails cleanly on a cable something else holds)")
    return 0


def card_flow(serial, rest):
    if not serial:
        cables = find_cables()
        msg = "--serial is required; upvc never picks a cable for you."
        if cables:
            msg += "\n  present: " + ", ".join(c["serial"] or "(no serial)" for c in cables)
            msg += "\n  (run `upvc list` for details)"
        die(msg, 2)
    if not has_rail_or_action(rest):
        die("give at least one rail (--vccint/--vccbram/--vccmem), or --show", 2)

    print("identifying the card ...")
    code, ident = run("ftdijtag.py", ["--serial", serial, "--idcode"], capture=True)
    ident = ident.strip()
    if code != 0:
        print(ident.splitlines()[-1] if ident else "(no output)", file=sys.stderr)
        die("could not read the IDCODE over FTDI %s" % serial, 1)
    print("  " + ident)
    m = re.search(r"PART (\S+)", ident)
    if not m:
        die("no part in the IDCODE reply")
    part = m.group(1)
    fam, bridge = family_for(part)
    if fam is None:
        die("unsupported part %s" % part)

    override = os.environ.get("SCVOLT_BIT", "").strip()
    if fam == "sc":
        if "--survey" in rest:
            die("--survey is for the TH53/TH55's PMBus; this card has a satellite controller")
        if "--enable-menu" in rest:
            if any(t.split("=")[0] in ("--vccint", "--vccbram", "--vccmem") for t in rest):
                die("--enable-menu sets no rails; run it on its own")
        else:
            code, out = run("scvolt.py", rest, capture=True)
            if code != 0:
                print(out, file=sys.stderr)
                return 2
            if not re.search(r"^frame: ", out, re.M):
                print(out, file=sys.stderr)
                die("no frame built")
    else:
        if "--enable-menu" in rest:
            die("--enable-menu is a satellite-controller command; a TH53/TH55/FK33 has none")
        code, guard = run("boardguard.py", ["--part", part, "--target", "ftdi:" + serial] + rest,
                          capture=True)
        sys.stdout.write(guard)
        if code != 0:
            return 4
        is_fk33 = "an FK33" in guard
        if fam == "fk33" and not is_fk33:
            die("a plain xcvu33p that the guard did not accept as an FK33")
        if is_fk33:
            fam = "fk33"
            bridge = "vrmbridge_fk33.bit.xz"
        code, _ = run("fk33set.py" if fam == "fk33" else "vrmset.py", ["--check"] + rest)
        if code != 0:
            return 2

    if fam == "sc":
        pci = pci_still_present()
        if pci:
            die("%s is still on the PCIe bus.\n\n  Programming over JTAG drops the link. "
                "Take it off first:\n      echo 1 | sudo tee /sys/bus/pci/devices/%s/remove\n"
                "  and put it back when you are done:\n      echo 1 | sudo tee /sys/bus/pci/rescan"
                % (pci, pci), 3)
    holders = jtag_holders()
    if holders:
        die("JTAG channel held by: %s\n  stop it with SIGTERM, never SIGKILL; a hard kill "
            "leaves all four FTDI channels marked open" % " ".join(holders))

    tmp = tempfile.mkdtemp(prefix="upvc-")
    try:
        if override:
            if not os.path.isfile(override):
                die("bitstream not found: %s" % override)
            raw = override
            print("  BIT %s (SCVOLT_BIT override, not checksummed)" % raw)
        else:
            xz = verified_bridge(bridge)
            print("  BIT %s (SHA256 ok)" % bridge)
            raw = unpack(xz, tmp)
        code, _ = run("ftdijtag.py", ["--serial", serial, "--load", raw])
        if code != 0:
            die("loading the bridge failed")
        script = {"sc": "scset.py", "vrm": "vrmset.py", "fk33": "fk33set.py"}[fam]
        code, _ = run(script, rest, serial=serial)
        return code
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_set(a, rest):
    extra = []
    for flag, val in (("--vccint", a.vccint), ("--vccbram", a.vccbram), ("--vccmem", a.vccmem)):
        if val is not None:
            extra += [flag, val]
    return card_flow(a.serial, extra + rest)


def cmd_show(a, rest):
    return card_flow(a.serial, ["--show"] + rest)


def cmd_limits(_a, _rest):
    for s in ("scvolt.py", "vrmset.py", "fk33set.py"):
        run(s, ["--limits"])
        print()
    return 0


def cmd_selftest(_a, _rest):
    for s, flag in (("scvolt.py", "--selfcheck"), ("vrmset.py", "--selftest"),
                    ("fk33set.py", "--selftest"), ("boardguard.py", "--selftest")):
        code, _ = run(s, [flag])
        if code != 0:
            return code
    return 0


def cmd_bits(_a, _rest):
    sums = read_sums(os.path.join(PREBUILT, "SHA256SUMS"))
    bad = 0
    for name in sorted(sums):
        p = os.path.join(PREBUILT, name)
        ok = os.path.isfile(p) and sha256(p) == sums[name]
        bad += 0 if ok else 1
        print("%-28s %s" % (name, "ok" if ok else "MISSING OR MISMATCH"))
    if os.path.isdir(BOARDS):
        for n in sorted(os.listdir(BOARDS)):
            if n.endswith(".bit.xz"):
                print("%-28s present (board image, no checksum list; not loaded by upvc)" % n)
    return 1 if bad else 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="upvc", description=__doc__.split("\n\n")[0],
        epilog="Anything upvc does not recognise after the subcommand is passed to the setter "
               "(e.g. --fk33-mod, --board, --settle, --survey, --out).")
    ap.add_argument("--version", action="version", version="upvc " + __version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("list", help="list FTDI 0403:6010/6011 cables (opens nothing unless --probe)")
    p.add_argument("--probe", action="store_true",
                   help="also read each cable's IDCODE/part (opens channel A if it is free)")
    p.set_defaults(fn=lambda a, r: cmd_list(a))

    def serial(q):
        q.add_argument("--serial", default=None,
                       help="FTDI serial of the cable (see `upvc list`); required, never defaulted")

    p = sub.add_parser("show", help="read the rails; loads the bridge, sets nothing")
    serial(p)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("set", help="set rails",
                       description="Each rail takes millivolts, `min` (the firmware/board floor) or "
                                   "`default` (stock). Which rails exist depends on the board; see "
                                   "`upvc limits`.")
    serial(p)
    for r in ("vccint", "vccbram", "vccmem"):
        p.add_argument("--" + r, default=None, metavar="mV|min|default")
    p.set_defaults(fn=cmd_set)

    for n, fn, h in (("limits", cmd_limits, "what each rail allows (no hardware)"),
                     ("selftest", cmd_selftest, "run the offline self-tests (no hardware)"),
                     ("bits", cmd_bits, "list the shipped bridges and check them against SHA256SUMS")):
        p = sub.add_parser(n, help=h)
        p.set_defaults(fn=fn)
    return ap


def main(argv=None):
    ap = build_parser()
    a, rest = ap.parse_known_args(argv)
    if a.cmd in ("list", "limits", "selftest", "bits") and rest:
        ap.error("unrecognized arguments: " + " ".join(rest))
    try:
        return a.fn(a, rest) or 0
    except Die as e:
        print("upvc: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    raise SystemExit(main())

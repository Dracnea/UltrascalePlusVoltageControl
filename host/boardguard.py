#!/usr/bin/env python3
"""Refuse Osprey E3xx boards before vrmbridge is loaded or the bus is touched.

    boardguard.py --part xcvu35p [--xvc host:port] [--board th55]
    boardguard.py --selftest

The Osprey E300 family (E309, E333, E335, and their C variants) is built on the
same dies as the TUL TH53 and TH55 -- a plain E335 module even reports the same
IDCODE as a TH55 (0x14b71093) -- so the part alone cannot tell them apart. But
the E3xx is a different board in every way that matters here: its rails belong
to the box's own controller, not to anything on the FPGA's pins, and loading any
bitstream onto one of its modules replaces the design it was running. So this
tool must not load its bridge on one, let alone send PMBus traffic.

Every check below runs before the bridge is loaded and sends nothing to the
FPGA. Any one of them is enough to refuse; there is no override.

  1. The die variant. The TH53 is xcvu33p_CIV and the TH55 is plain xcvu35p.
     A plain xcvu33p is an E333 module; an xcvu35p_CIV is an E335C.
  2. The JTAG server. An E3xx module's JTAG exists only as an XVC endpoint on
     the box's own Zynq: ports 2541-2543, answering `getinfo:` with
     `xvcServer_v1.0:2048`. A TH53/TH55 served over XVC from its own USB
     cable comes from a different server.
  3. The host behind it. An E3xx box answers its controller API on port 8200
     (`/controller/getAllInfo`) and serves an Osprey Electronics page on port 80.

A local USB cable needs no network checks: a TH53/TH55's JTAG is an on-board
FT4232H, and an E3xx module's JTAG is not wired to the host at all.

The SQRL FK33 is a plain xcvu33p, the same die as an E333 module. It passes
only on a local cable, and only when the JTAG target that was opened is an
FTDI whose USB product string is SQRL's ("SQRL FK"), matched by serial number.
An FK33 reached over XVC is refused: nothing on the far end can prove what it is.

The Alveo-class cards share IDCODEs with boards this tool has no bridge for:
xcu55n with the U50, xcu55c with the U280. On a local FTDI cable (the path that
needs no Vivado) the card's own USB product string decides: a C1100 / U55N
cable must name U55N or C1100, a U55C cable U55C. Anything else is refused.
"""
import argparse
import glob
import os
import socket
import sys
import urllib.request

OSPREY_XVC_PORTS = range(2541, 2544)
OSPREY_XVC_INFO = "xvcServer_v1.0:2048"
OSPREY_API_PORT = 8200
TIMEOUT = 3.0

# What each TH board's die must report, and what it would be if it were not.
EXPECT = {
    "th53": {"die": "vu33p", "civ": True,
             "other": "a plain xcvu33p is an Osprey E333 module, not a TH53 (xcvu33p_CIV)"},
    "th55": {"die": "vu35p", "civ": False,
             "other": "an xcvu35p_CIV is an Osprey E335C module, not a TH55 (plain xcvu35p)"},
}
SQRL_PRODUCT = "SQRL FK"
# Alveo-class die -> the words one of its cables' product strings must contain.
ALVEO_PRODUCTS = {"u55n": ("U55N", "C1100"), "u55c": ("U55C",)}


def ftdi_cables(root="/sys/bus/usb/devices"):
    """{serial: product} of every FTDI on this host, from sysfs (nothing is opened)."""
    out = {}
    for d in glob.glob(os.path.join(root, "*")):
        try:
            vid = open(os.path.join(d, "idVendor")).read().strip()
            prod = open(os.path.join(d, "product")).read().strip()
            ser = open(os.path.join(d, "serial")).read().strip()
        except OSError:
            continue
        if vid == "0403":
            out[ser] = prod
    return out


def sqrl_cables(root="/sys/bus/usb/devices"):
    """{serial: product} of every FTDI on this host whose product string is SQRL's."""
    return {s: p for s, p in ftdi_cables(root).items() if p.startswith(SQRL_PRODUCT)}


def alveo_die(part):
    p = (part or "").lower()
    return next((k for k in ALVEO_PRODUCTS if k in p), None)


def check_alveo(part, target, xvc=None, cables=None):
    """A C1100 / U55N or U55C on a local FTDI cable whose product string names
    it. Returns the die key, or raises Refused. Only the FTDI path calls this:
    through Vivado the part and the bridge are matched as before."""
    key = alveo_die(part)
    if key is None:
        raise Refused(f"part {part!r} is not a C1100 / U55N or U55C die")
    if xvc:
        raise Refused("the cable check needs a local cable; use the Vivado path over XVC")
    cables = ftdi_cables() if cables is None else cables
    hits = [s for s in cables if s and s in (target or "")]
    if len(hits) != 1:
        raise Refused(f"the JTAG target {target!r} is not an FTDI cable on this host "
                      f"(cables: {sorted(cables) or 'none'})")
    prod = cables[hits[0]]
    want = ALVEO_PRODUCTS[key]
    if not any(w in prod.upper() for w in want):
        raise Refused(f"cable {hits[0]} says {prod!r}, not {' / '.join(want)}: the {part} die is "
                      "shared with cards this tool has no bridge for")
    return key


def check_fk33(part, target, xvc=None, cables=None):
    """An FK33 is a plain xcvu33p on a local SQRL cable. Raises Refused."""
    p = (part or "").lower()
    if "vu33p" not in p or "_civ" in p:
        raise Refused(f"part {part!r} is not an FK33's die (plain xcvu33p)")
    if xvc:
        raise Refused("an FK33 must be on a local cable: over XVC a plain xcvu33p "
                      "cannot be told from an Osprey E333 module")
    cables = sqrl_cables() if cables is None else cables
    hits = [s for s in cables if s and s in (target or "")]
    if len(hits) != 1:
        raise Refused(f"the JTAG target {target!r} is not an SQRL FK cable "
                      f"(SQRL cables on this host: {sorted(cables) or 'none'}); a plain "
                      "xcvu33p on any other cable is treated as an E333 module")
    return "fk33"


class Refused(Exception):
    pass


def board_from_part(part):
    p = (part or "").lower()
    for key, e in EXPECT.items():
        if e["die"] in p:
            return key
    return None


def check_die(part, board=None):
    """Check 1. Returns the TH board the die belongs to, or raises Refused."""
    p = (part or "").lower()
    key = board_from_part(p)
    if key is None:
        raise Refused(f"part {part!r} is neither a TH53 nor a TH55 die")
    if board and board != key:
        raise Refused(f"--board {board} given, but the device is an {EXPECT[key]['die']} "
                      f"({part}), which would be a {key}")
    if ("_civ" in p) != EXPECT[key]["civ"]:
        raise Refused(f"{part}: {EXPECT[key]['other']}")
    return key


def xvc_getinfo(host, port, timeout=TIMEOUT):
    """The XVC server's `getinfo:` reply, or None if it cannot be asked."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"getinfo:")
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 64:
                chunk = s.recv(64)
                if not chunk:
                    break
                buf += chunk
            return buf.decode("ascii", "replace").strip()
    except OSError:
        return None


def _get(url, timeout=TIMEOUT):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(65536).decode("utf-8", "replace")
    except Exception:
        return None


def osprey_host(host, api_port=OSPREY_API_PORT, web_port=80, timeout=TIMEOUT):
    """Check 3. A description of the Osprey signature found on host, or None."""
    body = _get(f"http://{host}:{api_port}/controller/getAllInfo", timeout)
    if body and any(k in body for k in ("chipType", "voltage_vccint", "boardTemp", "mpsTemp")):
        return f"Osprey controller API answers on {host}:{api_port}"
    page = _get(f"http://{host}:{web_port}/", timeout)
    if page and "Osprey" in page:
        return f"Osprey web UI on {host}:{web_port}"
    return None


def split_xvc(xvc):
    host, _, port = xvc.strip().rpartition(":")
    if not host or not port.isdigit():
        raise Refused(f"SCLINK_XVC must be host:port, not {xvc!r}")
    return host.strip("[]"), int(port)


def check_xvc(xvc, api_port=OSPREY_API_PORT, web_port=80, getinfo=True):
    """Checks 2 and 3 for a card reached over XVC. Raises Refused.

    getinfo=False skips asking the XVC server itself -- for when a Vivado
    session already holds it and a second client would not be answered."""
    host, port = split_xvc(xvc)
    if getinfo:
        info = xvc_getinfo(host, port)
        if info is None:
            raise Refused(f"no XVC server answered on {host}:{port}")
        if info.startswith(OSPREY_XVC_INFO):
            raise Refused(f"{host}:{port} answers getinfo with {info!r}, the Osprey E3xx "
                          "box's own XVC server")
    found = osprey_host(host, api_port, web_port)
    if found:
        where = (f"and {port} is one of its module ports" if port in OSPREY_XVC_PORTS
                 else "so this is an Osprey E3xx box")
        raise Refused(f"{found}, {where}")


def guard(part, xvc=None, board=None, getinfo=True, target=None, cables=None, **ports):
    """All checks. Returns the board key, or raises Refused."""
    p = (part or "").lower()
    if board == "fk33" or (board is None and target and "vu33p" in p and "_civ" not in p):
        return check_fk33(part, target, xvc, cables)
    if board is None and alveo_die(p):
        return check_alveo(part, target, xvc, cables)
    key = check_die(part, board)
    if xvc:
        check_xvc(xvc, getinfo=getinfo, **ports)
    return key


def explain(e):
    return (f"REFUSED (board guard): {e}\n"
            "  This tool's PMBus path sets VCCINT on TUL TH53/TH55 boards only, and an "
            "Alveo-class card on a bare cable must be named by that cable. Osprey E3xx boards use "
            "the TH dies (an FK33 is accepted on its own SQRL cable only), but their "
            "rails belong to the box's own controller, and "
            "loading a bridge onto one of their modules would replace the design it is "
            "running. Nothing was loaded or sent.")


# --- selftest: fake E3xx and generic XVC endpoints on loopback ----------------
def selftest():
    import http.server
    import threading

    def xvc_server(reply):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                with c:
                    if c.recv(16).startswith(b"getinfo:"):
                        c.sendall(reply)
        threading.Thread(target=loop, daemon=True).start()
        return srv, srv.getsockname()[1]

    def http_server(body):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_):
                pass
        s = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return s, s.server_address[1]

    def closed_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    fails = 0

    def case(what, fn, want_refused):
        nonlocal fails
        try:
            fn()
            got = False
            msg = "accepted"
        except Refused as e:
            got = True
            msg = str(e)
        ok = got == want_refused
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}: {msg}")

    nothing = closed_port()
    quiet = {"api_port": nothing, "web_port": nothing}
    _, osprey_xvc = xvc_server(b"xvcServer_v1.0:2048\n")
    _, other_xvc = xvc_server(b"xvcServer_v1.0:16384\n")
    _, api = http_server('{"boards":[{"chipType":"vu35p_civ","voltage_vccint":850}]}')
    _, web = http_server("<title>Osprey Electronics</title>")

    case("TH55 die, local cable", lambda: guard("xcvu35p"), False)
    case("TH53 die, local cable", lambda: guard("xcvu33p_CIV"), False)
    case("E335C die (vu35p_CIV)", lambda: guard("xcvu35p_CIV"), True)
    case("E333 die (plain vu33p)", lambda: guard("xcvu33p"), True)
    case("--board disagrees with the die", lambda: guard("xcvu35p", board="th53"), True)
    case("not a TH die at all", lambda: guard("xcu50_u55n"), True)
    case("TH55 over another XVC server", lambda: guard("xcvu35p", xvc=f"127.0.0.1:{other_xvc}", **quiet), False)
    case("E335 XVC server signature", lambda: guard("xcvu35p", xvc=f"127.0.0.1:{osprey_xvc}", **quiet), True)
    case("other XVC server but Osprey API on the host",
         lambda: guard("xcvu35p", xvc=f"127.0.0.1:{other_xvc}", api_port=api, web_port=nothing), True)
    case("Osprey web UI on the host",
         lambda: guard("xcvu35p", xvc=f"127.0.0.1:{other_xvc}", api_port=nothing, web_port=web), True)
    case("XVC endpoint not answering", lambda: guard("xcvu35p", xvc=f"127.0.0.1:{nothing}", **quiet), True)
    case("Osprey API, getinfo skipped (link already open)",
         lambda: guard("xcvu35p", xvc=f"127.0.0.1:{other_xvc}", getinfo=False,
                       api_port=api, web_port=nothing), True)
    sqrl = {"FK0000000001": "SQRL FK"}
    case("FK33 on its SQRL cable", lambda: guard("xcvu33p", board="fk33",
         target="localhost:3121/xilinx_tcf/Xilinx/FK0000000001A", cables=sqrl), False)
    case("FK33 claimed, target is another cable", lambda: guard("xcvu33p", board="fk33",
         target="localhost:3121/xilinx_tcf/Xilinx/XFL000000000", cables=sqrl), True)
    case("FK33 claimed, no SQRL cable on the host", lambda: guard("xcvu33p", board="fk33",
         target="localhost:3121/xilinx_tcf/Xilinx/FK0000000001A", cables={}), True)
    case("plain vu33p on its SQRL cable, no --board", lambda: guard("xcvu33p",
         target="localhost:3121/xilinx_tcf/Xilinx/FK0000000001A", cables=sqrl), False)
    case("plain vu33p on another cable, no --board", lambda: guard("xcvu33p",
         target="localhost:3121/xilinx_tcf/Xilinx/XFL000000000", cables=sqrl), True)
    case("FK33 claimed over XVC", lambda: guard("xcvu33p", board="fk33",
         xvc="127.0.0.1:2542", target="", cables=sqrl), True)
    case("FK33 claimed on a TH53 die", lambda: guard("xcvu33p_CIV", board="fk33",
         target="localhost:3121/xilinx_tcf/Xilinx/FK0000000001A", cables=sqrl), True)
    alveo = {"XFL000000001": "A-U55C", "XFL000000002": "Varium C1100", "XFL000000003": "A-U50",
             "XFL000000004": "A-U280"}
    case("U55C on its own cable", lambda: guard("xcu280_u55c", target="ftdi:XFL000000001",
         cables=alveo), False)
    case("C1100 on its own cable", lambda: guard("xcu50_u55n", target="ftdi:XFL000000002",
         cables=alveo), False)
    case("xcu55n die on a U50 cable", lambda: guard("xcu50_u55n", target="ftdi:XFL000000003",
         cables=alveo), True)
    case("xcu55c die on a U280 cable", lambda: guard("xcu280_u55c", target="ftdi:XFL000000004",
         cables=alveo), True)
    case("U55C die, cable not on this host", lambda: guard("xcu280_u55c", target="ftdi:XFL999",
         cables=alveo), True)
    case("U55C over XVC", lambda: guard("xcu280_u55c", xvc="127.0.0.1:2542",
         target="ftdi:XFL000000001", cables=alveo), True)
    print(f"== {fails} failure(s)")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refuse Osprey E3xx boards before anything is loaded.")
    ap.add_argument("--part", default="")
    ap.add_argument("--xvc", default="")
    ap.add_argument("--board", default=None, choices=sorted(EXPECT) + ["fk33"])
    ap.add_argument("--target", default="", help="the opened JTAG target (FK33 check)")
    ap.add_argument("--xvc-only", action="store_true",
                    help="only the XVC checks (2, 3); for before the part is known")
    ap.add_argument("--selftest", action="store_true")
    a, _rest = ap.parse_known_args(argv)
    if a.selftest:
        return selftest()
    if a.xvc_only:
        try:
            check_xvc(a.xvc)
        except Refused as e:
            print(explain(e), file=sys.stderr)
            return 4
        print(f"board guard: {a.xvc} is not an Osprey E3xx box")
        return 0
    try:
        key = guard(a.part, xvc=a.xvc or None, board=a.board, target=a.target)
    except Refused as e:
        print(explain(e), file=sys.stderr)
        return 4
    if key == "fk33":
        print(f"board guard: {a.part} on SQRL cable {a.target}: an FK33")
        return 0
    if key in ALVEO_PRODUCTS:
        print(f"board guard: {a.part} on cable {a.target}: a {' / '.join(ALVEO_PRODUCTS[key])}")
        return 0
    print(f"board guard: {a.part} is a {key.upper()} die"
          + (f", and {a.xvc} is not an Osprey E3xx box" if a.xvc else ", on a local cable"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

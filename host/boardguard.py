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
"""
import argparse
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


def guard(part, xvc=None, board=None, getinfo=True, **ports):
    """All checks. Returns the TH board key, or raises Refused."""
    key = check_die(part, board)
    if xvc:
        check_xvc(xvc, getinfo=getinfo, **ports)
    return key


def explain(e):
    return (f"REFUSED (board guard): {e}\n"
            "  This tool sets VCCINT on TUL TH53/TH55 boards only. Osprey E3xx boards use "
            "the same dies, but their rails belong to the box's own controller, and "
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
    print(f"== {fails} failure(s)")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refuse Osprey E3xx boards before anything is loaded.")
    ap.add_argument("--part", default="")
    ap.add_argument("--xvc", default="")
    ap.add_argument("--board", default=None, choices=sorted(EXPECT))
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
        key = guard(a.part, xvc=a.xvc or None, board=a.board)
    except Refused as e:
        print(explain(e), file=sys.stderr)
        return 4
    print(f"board guard: {a.part} is a {key.upper()} die"
          + (f", and {a.xvc} is not an Osprey E3xx box" if a.xvc else ", on a local cable"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

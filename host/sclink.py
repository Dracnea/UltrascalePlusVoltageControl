#!/usr/bin/env python3
"""Talk to the satellite controller through the scbridge bitstream.

No vendor miner, no third-party bitstream, no background process to stop. This
drives rtl/scbridge_top.sv over JTAG BSCAN through Vivado's TCL mode, which is
the only external dependency.

THE BRIDGE REGISTER
    One BSCANE2 user register (USER1), 64 bits, shifted LSB first. Every shift
    writes a command and reads the result of the previous one, so a read is
    "send the command, then send a NOP and take what comes back".

        in   [7:0] op, [39:8] arg
        out  [63:0] result

        0x00 NOP    0x01 SYSMON  0x02 CONFIG  0x03 QUEUE
        0x04 RX     0x05 LINES   0x06 BURST   0x07 CLEAR

    See ../docs/protocol.md for the controller protocol carried over it.

SAFETY
    SYSMON, LINES and RX are pure reads; run them first. The controller command
    that enters the bootloader is not implemented here or in scvolt.py.
"""
import os
import subprocess
import sys
import time

IR_LEN = 12                      # xcu55n: two 6-bit SLR fields
_USER = {"USER1": 0x02, "USER2": 0x03, "USER3": 0x22}
_FILLER = 0x24


def ir(name, n=IR_LEN):
    slrs = max(1, n // 6)
    v = _USER[name] << (6 * (slrs - 1))
    for i in range(slrs - 1):
        v |= _FILLER << (6 * i)
    return f"{v:0{(n + 3) // 4}X}"


IR_USER1 = ir("USER1")           # the bridge's command register
MARK = "@@DONE@@"
CLK_HZ = 100_000_000

OP_NOP, OP_SYSMON, OP_CONFIG, OP_QUEUE, OP_RX, OP_LINES, OP_BURST, OP_CLEAR = (
    0, 1, 2, 3, 4, 5, 6, 7)


class ScLink:
    def __init__(self, vivado=None, serial=None):
        self.vivado = vivado or os.environ.get(
            "VIVADO", os.path.expanduser("~/Xilinx/2026.1/Vivado/bin/vivado"))
        # No default: a bench with more than one card must say which one, and a
        # baked-in serial is somebody else's board.
        self.serial = serial or os.environ.get("SCLINK_SERIAL", "").strip()
        self.proc = None

    # ---- vivado plumbing ----------------------------------------------------
    def _cmd(self, tcl, timeout=120):
        self.proc.stdin.write(tcl + "\nputs " + MARK + "\n")
        self.proc.stdin.flush()
        out, deadline = [], time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if MARK in line:
                return out
            out.append(line.rstrip("\n"))
        raise RuntimeError("vivado did not respond to: " + tcl.splitlines()[0])

    @staticmethod
    def _hexword(lines):
        for s in lines:
            s = s.strip().strip("{}")
            if s and all(c in "0123456789abcdefABCDEF" for c in s):
                return int(s, 16)
        return None

    def open(self):
        self.proc = subprocess.Popen(
            [self.vivado, "-mode", "tcl", "-nolog", "-nojournal", "-notrace"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._cmd("open_hw_manager", timeout=180)
        self._cmd("connect_hw_server -allow_non_jtag", timeout=180)
        # Pick the target by SERIAL: hw_server probes every FTDI on the host, so
        # index 0 is a coin flip on a bench with more than one card.
        if self.serial:
            self._cmd(f'set _t [lsearch -inline -glob [get_hw_targets] *{self.serial}*]')
            self._cmd(f'if {{$_t eq ""}} {{ error "no hw_target matching {self.serial}" }}')
        else:
            self._cmd('set _n [llength [get_hw_targets]]')
            self._cmd('if {$_n != 1} { error "expected one hw_target, found $_n --'
                      ' set SCLINK_SERIAL to choose" }')
            self._cmd('set _t [lindex [get_hw_targets] 0]')
        self._cmd("open_hw_target -jtag_mode 1 $_t", timeout=180)
        self._cmd("run_state_hw_jtag RESET")
        self._cmd("run_state_hw_jtag IDLE")
        return self

    def close(self):
        if not self.proc:
            return
        try:
            # Leaving the target open keeps the FTDI channel claimed and
            # recovery needs a USB replug.
            self.proc.stdin.write("close_hw_target\nclose_hw_manager\nexit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=25)
        except Exception:
            self.proc.kill()
        finally:
            self.proc = None

    def __enter__(self): return self.open()
    def __exit__(self, *_): self.close()

    # ---- the bridge register ----------------------------------------------
    def exchange(self, op, arg=0):
        """Issue one command and return the 64-bit result of the PREVIOUS shift.

        The register captures on the way in, so the answer to a command arrives
        on the shift after it. Sending the command then a NOP is the read.
        """
        word = (op & 0xFF) | ((arg & 0xFFFFFFFF) << 8)
        self._cmd(f"scan_ir_hw_jtag {IR_LEN} -tdi {IR_USER1}")
        out = self._cmd(f"puts [scan_dr_hw_jtag 64 -tdi {word:016x}]")
        return self._hexword(out) or 0

    def ask(self, op, arg=0):
        self.exchange(op, arg)
        return self.exchange(OP_NOP)

    # ---- what the bridge exposes -------------------------------------------
    def sysmon(self):
        r = self.ask(OP_SYSMON)
        if not r:
            return None
        temp, vccint, vccaux = r & 0xFFFF, (r >> 16) & 0xFFFF, (r >> 32) & 0xFFFF
        return {"temp_c": temp * 509.3140064 / 65536 - 280.23,
                "vccint_mv": round(vccint * 3000.0 / 65536),
                "vccaux_mv": round(vccaux * 3000.0 / 65536)}

    def lines(self):
        r = self.ask(OP_LINES)
        return {"txd_edges": r & 0xFFFF, "rxd_edges": (r >> 16) & 0xFFFF,
                "txd_level": bool(r >> 32 & 1), "rxd_level": bool(r >> 33 & 1),
                "gpio": (r >> 34) & 0xF}

    def configure(self, baud=115200, rx_pin=1, parity=True,
                  tx_pin=0, tx_parity=True, tx_odd=False):
        div = round(CLK_HZ / baud) & 0xFFFF
        arg = (div | (rx_pin << 16) | (int(parity) << 17) | (tx_pin << 18)
               | (int(tx_parity) << 19) | (int(tx_odd) << 20))
        return self.ask(OP_CONFIG, arg)

    def tx(self, data):
        """Queue a whole frame, then send it back-to-back.

        One JTAG round-trip costs tens of milliseconds, so a byte-per-round-trip
        transmitter spreads a frame over seconds and the controller times out
        between characters. The queue exists for exactly that reason.
        """
        self.exchange(OP_CLEAR)
        for b in data:
            self.exchange(OP_QUEUE, b)
        self.exchange(OP_BURST)
        time.sleep(0.002 + 0.0001 * len(data))   # ~87 us per 8E1 character

    def rx(self, limit=64):
        """Pop bytes until the bridge says the FIFO is empty."""
        out = bytearray()
        for _ in range(limit):
            r = self.ask(OP_RX)
            if not (r >> 8) & 1:
                break
            out.append(r & 0xFF)
        return len(out), bytes(out)

# --- the settled link parameters -------------------------------------------
# MEASURED 2026-09-14 by sweeping both pins against all three parities: an
# identify sent on pin 0 at 8E1 and listened for on pin 1 draws 0x83 with a
# valid checksum. 8N1 returns garbage, 8O1 returns silence. This is the ECU200
# result holding on the C1100, and it is why the controller looked mute for so
# long -- it had only ever been spoken to 8N1.
TX_PIN, RX_PIN, PARITY, ODD = 0, 1, True, False


class ScSession:
    """The satellite controller's session, over the link."""

    def __init__(self, link):
        self.l = link
        self.l.configure(baud=115200, rx_pin=RX_PIN, parity=PARITY,
                         tx_pin=TX_PIN, tx_parity=PARITY, tx_odd=ODD)

    def talk(self, frame, wait=0.6, retries=2):
        """Send one SC frame, return (cmd, data) of the reply, or None."""
        import scvolt
        for _ in range(retries):
            self.l.configure(baud=115200, rx_pin=RX_PIN, parity=PARITY,
                             tx_pin=TX_PIN, tx_parity=PARITY, tx_odd=ODD)
            self.l.tx(frame)
            time.sleep(wait)
            _, data = self.l.rx()
            buf = bytes(data)
            i = buf.find(b"\x5c\x02")
            j = buf.find(b"\x5c\x03", i + 2) if i >= 0 else -1
            if i >= 0 and 0 < j - i < 64:
                try:
                    return scvolt.parse(buf[i:j + 2])
                except Exception:
                    pass
        return None

    def handshake(self):
        """identify -> comm version -> sensors -> firmware version."""
        import scvolt
        out = {}
        r = self.talk(scvolt.identify())
        if not r or r[0] != 0x83:
            return None
        out["identify"] = r[1]
        out["comm_version"] = self.talk(scvolt.comm_version(9))
        out["sensors"] = self.talk(scvolt.sensors())
        fw = self.talk(scvolt.fw_version())
        out["fw"] = fw
        # A stock controller never answers 0x71, which is what gates rail
        # setting; 0x72 is the has-TRM-firmware flag.
        out["trm_firmware"] = bool(fw and fw[0] == 0x72)
        return out


def main():
    import scvolt
    with ScLink() as link:
        print("== SYSMON (on-die, no satellite controller involved)")
        sm = link.sysmon()
        print("  ", sm if sm else "no reply — is the bridge bitstream loaded?")
        if not sm:
            return 1

        print("== line direction (PULLDOWN test: 1 = the pin is driven)")
        ln = link.lines()
        print("  ", ln)

        if "--listen-only" in sys.argv:
            link.configure()
            print("== listening 8E1 for 5 s ...")
            time.sleep(5)
            print("  rx:", link.rx())
            return 0

        for parity, name in ((True, "8E1"), (False, "8N1")):
            print(f"== identify at 115200 {name}")
            link.configure(parity=parity, tx_parity=parity)
            link.tx(scvolt.identify())
            time.sleep(0.4)
            n, data = link.rx()
            print(f"   rx {n} bytes: {data.hex() if n else '(silence)'}")
            if n:
                try:
                    cmd, payload = scvolt.parse(bytes(data))
                    print(f"   PARSED cmd=0x{cmd:02x} data={payload.hex()}")
                except Exception as e:
                    print("   unparsed:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

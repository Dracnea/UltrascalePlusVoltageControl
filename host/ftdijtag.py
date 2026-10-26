#!/usr/bin/env python3
"""The bridge register over an FTDI cable directly -- no Vivado, no hw_server.

    SCLINK_FTDI=<serial> fk33set.py ...      use this instead of host/sclink.py
    ftdijtag.py --serial S --idcode          read the IDCODE and exit
    ftdijtag.py --serial S --load f.bit      configure the FPGA (full .bit) and check DONE

Speaks MPSSE to channel A of an FT2232H / FT4232H through libftdi1 (ctypes,
standard library only), and offers the same face as sclink.ScLink: `part`,
`exchange()`, `ask()` and `sysmon()`. It shifts the bridge's 64-bit USER1
register, and can load a full bitstream (the bridge) with the standard JTAG
configuration sequence: JPROGRAM, wait for INIT, CFG_IN, JSTART, then DONE.

Pins are the usual MPSSE JTAG mapping (ADBUS0 TCK, 1 TDI, 2 TDO, 3 TMS), and
ADBUS5-7 are driven high as SQRL's and AMD's cards expect of their JTAG FTDI.
TCK is 6 MHz.

Only one process may hold an FTDI channel. Opening fails cleanly if Vivado,
hw_server or another tool already has it; never SIGKILL a holder.
"""
import argparse
import ctypes
import ctypes.util
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VID = 0x0403
PIDS = (0x6010, 0x6011)          # FT2232H, FT4232H
INTERFACE_A = 1
BITMODE_RESET, BITMODE_MPSSE = 0x00, 0x02
TCK_DIV = 4                      # 60 MHz / (2 * (4 + 1)) = 6 MHz

OP_NOP, OP_SYSMON = 0x00, 0x01

# UltraScale+ 6-bit JTAG instructions (UG570).
IR_CFG_IN, IR_JPROGRAM, IR_JSTART, IR_ISC_NOOP = 0x05, 0x0B, 0x0C, 0x14
IR_DONE_BIT = 0x20             # the IR capture value: bit 5 is DONE
IR_INIT_BIT = 0x10             # bit 4 is INIT_COMPLETE

REV8 = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def bit_payload(path):
    """The configuration bytes of a .bit file (after its header) and its part name."""
    with open(path, "rb") as f:
        data = f.read()
    hlen = (data[0] << 8) | data[1]
    p = 2 + hlen + 2
    part = ""
    while p < len(data):
        key = data[p]; p += 1
        if key == ord("e"):
            n = int.from_bytes(data[p:p + 4], "big")
            return data[p + 4:p + 4 + n], part
        n = (data[p] << 8) | data[p + 1]; p += 2
        if key == ord("b"):
            part = data[p:p + n].rstrip(b"\0").decode("ascii", "replace")
        p += n
    raise FtdiError(f"{path}: no configuration payload")

# IDCODE (version nibble masked) -> the part string Vivado would report, for
# the dies this tool knows. The 6-bit-per-SLR IR rule is the same as sclink's.
PARTS = {
    0x04B69093: ("xcvu33p", 6),
    0x04B6D093: ("xcvu33p_CIV", 6),
    0x04B71093: ("xcvu35p", 12),
    0x04B77093: ("xcu50_u55n", 12),
}


def _lib():
    name = ctypes.util.find_library("ftdi1") or "libftdi1.so.2"
    try:
        lib = ctypes.CDLL(name)
    except OSError as e:
        raise RuntimeError(f"libftdi1 not found ({e}); install libftdi1-2") from e
    lib.ftdi_new.restype = ctypes.c_void_p
    for f in ("ftdi_set_interface", "ftdi_usb_open_desc", "ftdi_usb_reset", "ftdi_set_bitmode",
              "ftdi_set_latency_timer", "ftdi_write_data", "ftdi_read_data", "ftdi_usb_close",
              "ftdi_tcioflush"):
        if hasattr(lib, f):
            getattr(lib, f).restype = ctypes.c_int
    lib.ftdi_get_error_string.restype = ctypes.c_char_p
    return lib


class FtdiError(RuntimeError):
    pass


class FtdiLink:
    """sclink.ScLink's interface, over MPSSE."""

    def __init__(self, serial=None):
        self.serial = serial or os.environ.get("SCLINK_FTDI", "").strip()
        if not self.serial:
            raise FtdiError("no FTDI serial given (SCLINK_FTDI); refusing to pick a cable")
        self.ctx = None
        self.lib = None
        self.part = ""
        self.ir_len = 6
        self.idcode = None

    # ---- the cable ----------------------------------------------------------
    def _chk(self, rc, what):
        if rc < 0:
            msg = self.lib.ftdi_get_error_string(ctypes.c_void_p(self.ctx)).decode("ascii", "replace")
            raise FtdiError(f"{what}: {msg} ({rc})")
        return rc

    def open(self):
        self.lib = _lib()
        self.ctx = self.lib.ftdi_new()
        if not self.ctx:
            raise FtdiError("ftdi_new failed")
        c = ctypes.c_void_p(self.ctx)
        self._chk(self.lib.ftdi_set_interface(c, INTERFACE_A), "set interface A")
        ser = self.serial.encode()
        rc = -1
        for pid in PIDS:
            rc = self.lib.ftdi_usb_open_desc(c, VID, pid, None, ctypes.c_char_p(ser))
            if rc == 0:
                break
        if rc != 0:
            msg = self.lib.ftdi_get_error_string(c).decode("ascii", "replace")
            busy = "busy" in msg.lower() or "claim" in msg.lower() or rc in (-5, -6)
            raise FtdiError(("JTAG busy: another process holds this FTDI channel" if busy
                             else f"cannot open FTDI serial {self.serial}") + f" ({msg})")
        self._opened = True
        self._chk(self.lib.ftdi_usb_reset(c), "reset")
        self._chk(self.lib.ftdi_set_latency_timer(c, 1), "latency")
        self._chk(self.lib.ftdi_set_bitmode(c, 0, BITMODE_RESET), "bitmode reset")
        self._chk(self.lib.ftdi_set_bitmode(c, 0, BITMODE_MPSSE), "bitmode MPSSE")
        time.sleep(0.02)
        self._drain()
        # no div5, no adaptive clocking, no 3-phase; TCK divisor; ADBUS: TCK/TDI/TMS out,
        # TDO in, ADBUS5-7 high and out (val 0xE8, dir 0xEB).
        self._write(bytes([0x8A, 0x97, 0x8D, 0x86, TCK_DIV & 0xFF, TCK_DIV >> 8, 0x80, 0xE8, 0xEB]))
        self._tms(5, 0b11111)            # Test-Logic-Reset
        self._tms(1, 0b0)                # Run-Test/Idle
        self.idcode = self._read_idcode()
        part = PARTS.get(self.idcode & 0x0FFFFFFF)
        if part is None:
            raise FtdiError(f"IDCODE 0x{self.idcode:08x} is not a die this tool knows")
        self.part, self.ir_len = part
        return self

    def close(self):
        if self.ctx:
            c = ctypes.c_void_p(self.ctx)
            try:
                if getattr(self, "_opened", False):
                    self.lib.ftdi_set_bitmode(c, 0, BITMODE_RESET)
                    self.lib.ftdi_usb_close(c)
            finally:
                self.lib.ftdi_free(c)
                self.ctx = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    def _write(self, buf):
        b = (ctypes.c_ubyte * len(buf)).from_buffer_copy(buf)
        n = self._chk(self.lib.ftdi_write_data(ctypes.c_void_p(self.ctx), b, len(buf)), "write")
        if n != len(buf):
            raise FtdiError(f"short write {n}/{len(buf)}")

    def _read(self, want, timeout=3.0):
        out = bytearray()
        buf = (ctypes.c_ubyte * 4096)()
        end = time.monotonic() + timeout
        while len(out) < want:
            n = self._chk(self.lib.ftdi_read_data(ctypes.c_void_p(self.ctx), buf, want - len(out)), "read")
            out += bytes(buf[:n])
            if len(out) < want:
                if time.monotonic() > end:
                    raise FtdiError(f"read timeout {len(out)}/{want}")
                time.sleep(0.001)
        return bytes(out)

    def _drain(self):
        buf = (ctypes.c_ubyte * 4096)()
        for _ in range(4):
            if self.lib.ftdi_read_data(ctypes.c_void_p(self.ctx), buf, 4096) <= 0:
                break

    # ---- JTAG ---------------------------------------------------------------
    def _tms(self, n, bits, tdi=0):
        """Clock n (1..7) TMS bits, LSB first, holding TDI at `tdi`."""
        self._write(bytes([0x4B, n - 1, (bits & 0x7F) | ((tdi & 1) << 7)]))

    def _shift(self, value, nbits, ir=False, read=True):
        """From Run-Test/Idle: shift nbits of value into IR or DR and back to
        Run-Test/Idle, returning what came out of TDO (LSB first)."""
        self._tms(4 if ir else 3, 0b0011 if ir else 0b001)        # -> Shift-IR / Shift-DR
        body = nbits - 1
        nbytes, rbits = divmod(body, 8)
        cmd = bytearray()
        rd = 0x39 if read else 0x19                                 # bytes, -ve out, +ve in, LSB first
        if nbytes:
            cmd += bytes([rd, (nbytes - 1) & 0xFF, (nbytes - 1) >> 8])
            cmd += bytes((value >> (8 * i)) & 0xFF for i in range(nbytes))
        if rbits:
            cmd += bytes([0x3B if read else 0x1B, rbits - 1, (value >> (8 * nbytes)) & 0xFF])
        last = (value >> body) & 1
        cmd += bytes([0x6B if read else 0x4B, 0, 0x01 | (last << 7)])  # last bit with TMS=1 -> Exit1
        cmd += bytes([0x4B, 1, 0b01])                                   # Update, Run-Test/Idle
        if read:
            cmd += bytes([0x87])
        self._write(bytes(cmd))
        if not read:
            return 0
        got = self._read(nbytes + (1 if rbits else 0) + 1)
        out = 0
        for i in range(nbytes):
            out |= got[i] << (8 * i)
        k = nbytes
        if rbits:
            out |= (got[k] >> (8 - rbits)) << (8 * nbytes)
            k += 1
        out |= ((got[k] >> 7) & 1) << body
        return out

    def _read_idcode(self):
        self._tms(5, 0b11111)
        self._tms(1, 0b0)
        return self._shift(0, 32)            # IDCODE is the DR after Test-Logic-Reset

    def _user1(self):
        """USER1 for this IR length: 0x02 per SLR's 6 bits, BYPASS (all ones)
        elsewhere -- sclink.ir()'s rule, for the one-SLR dies this serves."""
        if self.ir_len != 6:
            raise FtdiError(f"IR length {self.ir_len}: only one-SLR dies are supported here")
        return 0x02

    # ---- configuration -------------------------------------------------------
    def _ir_capture(self, op=IR_ISC_NOOP):
        return self._shift(op, self.ir_len, ir=True)

    def _idle(self, cycles):
        while cycles > 0:
            n = min(cycles, 65536 * 8)
            nb = max(1, n // 8)
            self._write(bytes([0x8F, (nb - 1) & 0xFF, (nb - 1) >> 8]))      # clock nb*8 TCKs, no data
            cycles -= nb * 8

    def load_bit(self, path, log=print):
        """Configure the FPGA with a full bitstream. Raises unless DONE goes high."""
        payload, part = bit_payload(path)
        if part and not part.lower().startswith(self.part.lower().split("_")[0]):
            raise FtdiError(f"{path} is built for {part}, the device is {self.part}")
        if self.ir_len != 6:
            raise FtdiError("loading is implemented for one-SLR dies only")
        log(f"loading {os.path.basename(path)} ({len(payload)} bytes, part {part or '?'}) ...")
        self._shift(IR_JPROGRAM, 6, ir=True, read=False)
        t0 = time.monotonic()
        while True:                                    # INIT_COMPLETE after the clear
            time.sleep(0.01)
            if self._ir_capture() & IR_INIT_BIT:
                break
            if time.monotonic() - t0 > 2.0:
                raise FtdiError("INIT_COMPLETE never rose after JPROGRAM")
        self._idle(10000)
        self._shift(IR_CFG_IN, 6, ir=True, read=False)
        self._tms(3, 0b001)                            # Run-Test/Idle -> Shift-DR
        data = payload.translate(REV8)                 # config words go MSB first; MPSSE shifts LSB first
        body, last = data[:-1], data[-1]
        for off in range(0, len(body), 32768):
            chunk = body[off:off + 32768]
            self._write(bytes([0x19, (len(chunk) - 1) & 0xFF, (len(chunk) - 1) >> 8]) + chunk)
        self._write(bytes([0x1B, 6, last & 0x7F]))     # 7 bits, then the 8th with TMS -> Exit1-DR
        self._write(bytes([0x4B, 0, 0x01 | ((last >> 7) & 1) << 7]))
        self._tms(2, 0b01)                             # Update-DR, Run-Test/Idle
        self._shift(IR_JSTART, 6, ir=True, read=False)
        self._idle(2000)
        cap = self._ir_capture()
        self._tms(5, 0b11111)
        self._tms(1, 0b0)
        if not cap & IR_DONE_BIT:
            raise FtdiError(f"DONE did not go high (IR capture 0x{cap:02x})")
        log(f"loaded (DONE high, IR capture 0x{cap:02x}, {time.monotonic() - t0:.1f} s)")

    # ---- the bridge register (sclink.ScLink's face) -------------------------
    def exchange(self, op, arg=0):
        word = (op & 0xFF) | ((arg & 0xFFFFFFFFFFFF) << 8)
        self._shift(self._user1(), self.ir_len, ir=True, read=False)
        return self._shift(word, 64)

    def ask(self, op, arg=0):
        self.exchange(op, arg)
        return self.exchange(OP_NOP)

    def sysmon(self):
        r = self.ask(OP_SYSMON)
        if not r:
            return None
        temp, vccint, vccaux = r & 0xFFFF, (r >> 16) & 0xFFFF, (r >> 32) & 0xFFFF
        vccbram = (r >> 48) & 0xFFFF
        return {"temp_c": temp * 509.3140064 / 65536 - 280.23,
                "vccint_mv": round(vccint * 3000.0 / 65536),
                "vccaux_mv": round(vccaux * 3000.0 / 65536),
                "vccbram_mv": round(vccbram * 3000.0 / 65536) if vccbram else None}


def link():
    """The link this run should use: FtdiLink with SCLINK_FTDI set, else Vivado's."""
    if os.environ.get("SCLINK_FTDI", "").strip():
        return FtdiLink()
    import sclink
    return sclink.ScLink()


def main(argv=None):
    ap = argparse.ArgumentParser(description="FTDI MPSSE link to the bridge register.")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--idcode", action="store_true")
    ap.add_argument("--load", default=None, help="configure the FPGA with this .bit")
    a = ap.parse_args(argv)
    try:
        with FtdiLink(a.serial) as l:
            print(f"IDCODE 0x{l.idcode:08x}  PART {l.part}  IR {l.ir_len}")
            if a.load:
                l.load_bit(a.load)
    except FtdiError as e:
        print(f"ftdijtag: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

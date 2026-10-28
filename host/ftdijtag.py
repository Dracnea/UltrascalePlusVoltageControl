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

Multi-SLR dies (the C1100 / U55N's two SLRs, the U55C's three) concatenate one
6-bit IR per SLR. The instruction rides in the top field and every lower SLR
gets 0x24 -- the rule sclink.ir() applies through Vivado -- except BYPASS, which
is all ones across the full width. The IR length is measured on the chain and
must agree with the part table, or nothing is shifted.

TCK is clocked with zero-data byte shifts, not MPSSE's clock-only command: on
the FT4232H that the Alveo and TUL cards carry, the clock-only command does
not toggle TCK. DONE is read from the configuration STAT register (CFG_OUT),
which every SLR count reports the same way.

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
import sclink  # noqa: E402

VID = 0x0403
PIDS = (0x6010, 0x6011)          # FT2232H, FT4232H
INTERFACE_A = 1
BITMODE_RESET, BITMODE_MPSSE = 0x00, 0x02
TCK_DIV = 4                      # 60 MHz / (2 * (4 + 1)) = 6 MHz

# UltraScale+ 6-bit JTAG instructions (UG570), per SLR.
IR_CFG_OUT, IR_CFG_IN, IR_JPROGRAM, IR_JSTART, IR_ISC_NOOP = 0x04, 0x05, 0x0B, 0x0C, 0x14
IR_USER1, IR_BYPASS = 0x02, 0x3F
IR_FILLER = 0x24               # what every lower SLR's field carries
IR_DONE_BIT = 0x20             # the IR capture value: bit 5 is DONE
IR_INIT_BIT = 0x10             # bit 4 is INIT_COMPLETE
STAT_DONE, STAT_EOS, STAT_CRC_ERR = 1 << 14, 1 << 4, 1 << 0

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

# IDCODE (version nibble masked) -> the part string Vivado would report, the
# IR length (6 bits per SLR), and the part prefixes a .bit header may name for
# it. One IDCODE can be several dies (xcu50 / xcu55n, xcu280 / xcu55c); the
# board guard tells those apart by the cable, not this table.
PARTS = {
    0x04B69093: ("xcvu33p", 6, ("xcvu33p-",)),
    0x04B6D093: ("xcvu33p_CIV", 6, ("xcvu33p_civ-",)),
    0x04B71093: ("xcvu35p", 12, ("xcvu35p-",)),
    0x04B77093: ("xcu50_u55n", 12, ("xcu55n-", "xcu50-")),
    0x04B7D093: ("xcu280_u55c", 18, ("xcu55c-", "xcu280-")),
}


def ir_word(op, ir_len):
    """A 6-bit instruction widened to an ir_len-bit chain (sclink.ir()'s rule)."""
    slrs = max(1, ir_len // 6)
    if op == IR_BYPASS:
        return (1 << (6 * slrs)) - 1
    v = op << (6 * (slrs - 1))
    for i in range(slrs - 1):
        v |= IR_FILLER << (6 * i)
    return v


def _lib():
    # UPVC_LIBFTDI names the library outright (a libftdi1.dll on Windows).
    name = (os.environ.get("UPVC_LIBFTDI") or ctypes.util.find_library("ftdi1")
            or "libftdi1.so.2")
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


class FtdiLink(sclink.BridgeOps):
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
        self.bit_parts = ()

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
        self.part, self.ir_len, self.bit_parts = part
        measured = self._measure_ir_len()
        if measured != self.ir_len:
            raise FtdiError(f"IDCODE 0x{self.idcode:08x} is {self.part} (IR {self.ir_len}), but the "
                            f"chain measures IR {measured}: more than one device, or a bad cable")
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

    def _measure_ir_len(self):
        """Flush the IR with zeros, then feed ones and count to the first one out.
        Needs no part table; leaves the TAP in Run-Test/Idle with nothing loaded."""
        self._tms(5, 0b11111)
        self._tms(5, 0b00110)                          # TLR -> RTI -> Select-DR -> Select-IR -> Capture -> Shift-IR
        self._write(bytes([0x19, 7, 0]) + bytes(8))    # 64 zeros
        n = None
        for i in range(64):
            self._write(bytes([0x3B, 0, 0x01, 0x87]))  # one 1 in, one bit out
            if (self._read(1)[0] >> 7) & 1:
                n = i
                break
        self._tms(5, 0b11111)                          # -> Test-Logic-Reset; nothing was updated
        self._tms(1, 0b0)
        if n is None:
            raise FtdiError("could not measure the IR length (TDO never echoed a one)")
        return n

    def _ir(self, op, read=False):
        return self._shift(ir_word(op, self.ir_len), self.ir_len, ir=True, read=read)

    def _user1(self):
        return ir_word(IR_USER1, self.ir_len)

    # ---- configuration -------------------------------------------------------
    def _ir_capture(self, op=IR_ISC_NOOP):
        """The status the IR captures (bit 4 INIT_COMPLETE, bit 5 DONE) of the
        SLR nearest TDO."""
        return self._ir(op, read=True) & 0x3F

    def _idle(self, cycles):
        """Clock TCK in Run-Test/Idle by shifting zero bytes (see the module note)."""
        nb = max(1, (cycles + 7) // 8)
        while nb > 0:
            n = min(nb, 65536)
            self._write(bytes([0x19, (n - 1) & 0xFF, (n - 1) >> 8]) + bytes(n))
            nb -= n

    def _shift_dr_bytes(self, data):
        """Run-Test/Idle -> Shift-DR, all of data LSB first, -> Run-Test/Idle."""
        self._tms(3, 0b001)
        body, last = data[:-1], data[-1]
        for off in range(0, len(body), 32768):
            chunk = body[off:off + 32768]
            self._write(bytes([0x19, (len(chunk) - 1) & 0xFF, (len(chunk) - 1) >> 8]) + chunk)
        self._write(bytes([0x1B, 6, last & 0x7F]))     # 7 bits, then the 8th with TMS -> Exit1-DR
        self._write(bytes([0x4B, 0, 0x01 | ((last >> 7) & 1) << 7]))
        self._tms(2, 0b01)                             # Update-DR, Run-Test/Idle

    def read_stat(self):
        """The configuration STAT register (type-1 read of reg 7 over CFG_IN /
        CFG_OUT). Non-destructive."""
        self._tms(5, 0b11111)
        self._tms(1, 0b0)
        self._ir(IR_CFG_IN)
        words = (0xFFFFFFFF, 0xAA995566, 0x20000000, 0x2800E001, 0x20000000, 0x20000000)
        self._shift_dr_bytes(b"".join(w.to_bytes(4, "big") for w in words).translate(REV8))
        self._ir(IR_CFG_OUT)
        raw = self._shift(0, 32).to_bytes(4, "little").translate(REV8)
        return int.from_bytes(raw, "big")

    def _configure(self, payload):
        self._tms(5, 0b11111)
        self._tms(1, 0b0)
        self._ir(IR_JPROGRAM)
        self._ir(IR_ISC_NOOP)
        t0 = time.monotonic()
        while True:                                    # INIT_COMPLETE after the clear, TCK running
            self._idle(80000)
            if self._ir_capture() & IR_INIT_BIT:
                break
            if time.monotonic() - t0 > 3.0:
                raise FtdiError("INIT_COMPLETE never rose after JPROGRAM")
        self._idle(300000)
        self._ir(IR_ISC_NOOP)
        self._ir(IR_CFG_IN)
        self._idle(10000)
        self._shift_dr_bytes(payload.translate(REV8))  # config words go MSB first; MPSSE shifts LSB first
        self._idle(200000)                             # the startup sequence runs on these
        self._ir(IR_JSTART)
        self._idle(4000)
        self._tms(5, 0b11111)
        self._tms(1, 0b0)
        return self.read_stat()

    def load_bit(self, path, log=print):
        """Configure the FPGA with a full bitstream. Raises unless STAT shows
        DONE with no CRC error; one retry, as a cold card's first load can be
        lost to a flash boot the config engine is still retrying."""
        payload, part = bit_payload(path)
        if part and not part.lower().startswith(self.bit_parts):
            raise FtdiError(f"{path} is built for {part}, the device is {self.part}")
        log(f"loading {os.path.basename(path)} ({len(payload)} bytes, part {part or '?'}, "
            f"IR {self.ir_len}) ...")
        t0 = time.monotonic()
        for attempt in (1, 2):
            st = self._configure(payload)
            if st & STAT_DONE and not st & STAT_CRC_ERR:
                break
            log(f"  attempt {attempt}: STAT 0x{st:08x} (DONE {int(bool(st & STAT_DONE))}, "
                f"CRC_ERR {st & STAT_CRC_ERR})")
        else:
            raise FtdiError(f"configuration did not complete (STAT 0x{st:08x})")
        log(f"loaded (STAT 0x{st:08x}: DONE, EOS {int(bool(st & STAT_EOS))}, "
            f"{time.monotonic() - t0:.1f} s)")

    # ---- the bridge register (sclink.ScLink's face) -------------------------
    def exchange(self, op, arg=0):
        word = (op & 0xFF) | ((arg & 0xFFFFFFFFFFFF) << 8)
        self._shift(self._user1(), self.ir_len, ir=True, read=False)
        return self._shift(word, 64)


def link():
    """The link this run should use: FtdiLink with SCLINK_FTDI set, else Vivado's."""
    if os.environ.get("SCLINK_FTDI", "").strip():
        return FtdiLink()
    return sclink.ScLink()


def main(argv=None):
    ap = argparse.ArgumentParser(description="FTDI MPSSE link to the bridge register.")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--idcode", action="store_true")
    ap.add_argument("--load", default=None, help="configure the FPGA with this .bit")
    ap.add_argument("--stat", action="store_true", help="read the configuration STAT register")
    a = ap.parse_args(argv)
    try:
        with FtdiLink(a.serial) as l:
            print(f"IDCODE 0x{l.idcode:08x}  PART {l.part}  IR {l.ir_len}")
            if a.load:
                l.load_bit(a.load)
            if a.stat:
                st = l.read_stat()
                print(f"STAT 0x{st:08x}  DONE {int(bool(st & STAT_DONE))}  "
                      f"EOS {int(bool(st & STAT_EOS))}  CRC_ERR {st & STAT_CRC_ERR}")
    except (FtdiError, RuntimeError) as e:
        print(f"ftdijtag: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

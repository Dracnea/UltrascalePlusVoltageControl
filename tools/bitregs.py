#!/usr/bin/env python3
"""List the configuration-register writes in an UltraScale+ .bit, frame data excluded.

Two images made from the same design must agree on every register except the
ones that address or check frame data (FAR, FDRI, CRC, CMD, MFWR). Comparing
them is how an ECO'd image is shown to carry the original's USR_ACCESS stamp,
COR settings and IDCODE:

    bitregs.py original.bit              # print the register writes
    bitregs.py original.bit new.bit      # diff, exit 1 on any difference
"""
import sys

NAMES = {0x00: "CRC", 0x01: "FAR", 0x02: "FDRI", 0x04: "CMD", 0x05: "CTL0",
         0x06: "MASK", 0x09: "COR0", 0x0A: "MFWR", 0x0B: "CBC", 0x0C: "IDCODE",
         0x0D: "AXSS/USR_ACCESS", 0x0E: "COR1", 0x10: "WBSTAR", 0x11: "TIMER",
         0x18: "CTL1", 0x1E: "BOUT", 0x1F: "BSPI"}
FRAME_REGS = {0x00, 0x01, 0x02, 0x04, 0x0A}


def writes(path):
    data = open(path, "rb").read()
    sync = data.find(b"\xaa\x99\x55\x66")
    if sync < 0:
        sys.exit(f"{path}: no sync word")
    words = [int.from_bytes(data[i:i + 4], "big") for i in range(sync, len(data) - 3, 4)]
    out, i, reg = [], 0, None
    while i < len(words):
        w = words[i]; i += 1
        typ = w >> 29
        if typ == 1:
            op, reg, n = (w >> 27) & 3, (w >> 13) & 0x3FFF, w & 0x7FF
        elif typ == 2:
            op, n = (w >> 27) & 3, w & 0x7FFFFFF
        else:
            continue                              # sync, NOOP-like, dummy words
        if op != 2 or n == 0:
            continue
        if reg == 0x02 or (typ == 2 and reg != 0x1E):
            i += n                                # frame data: skip it
        elif reg == 0x1E:
            continue                              # an SLR's own bitstream: parse inside it
        elif n == 1:
            out.append((reg, words[i])); i += 1
        else:
            i += n
    return out


def show(ws):
    return [f"{NAMES.get(r, hex(r)):16s} {v:08x}" for r, v in ws if r not in FRAME_REGS]


if len(sys.argv) == 2:
    print("\n".join(show(writes(sys.argv[1]))))
else:
    a, b = show(writes(sys.argv[1])), show(writes(sys.argv[2]))
    if a == b:
        print(f"IDENTICAL: {len(a)} register writes outside frame data")
    else:
        import difflib
        print("\n".join(difflib.unified_diff(a, b, sys.argv[1], sys.argv[2], lineterm="")))
        sys.exit(1)

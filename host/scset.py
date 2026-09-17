#!/usr/bin/env python3
"""Set the C1100's rails and prove it happened.

    scset.py --vccint 720 --vccbram min --vccmem min
    scset.py --show                       read the rails, change nothing
    scset.py --enable-menu                start the controller's own test menu

The bridge bitstream must already be loaded — changeVoltage.sh does that. The
verification is SYSMON, which reads VCCINT on the die itself: it does not
consult the satellite controller, so it is an independent witness rather than
the controller agreeing with itself.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sclink  # noqa: E402
import scvolt  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vccint", default=None)
    ap.add_argument("--vccbram", default=None)
    ap.add_argument("--vccmem", default=None)
    ap.add_argument("--fw", default=None, help="override the detected SC firmware")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--enable-menu", action="store_true",
                    help="send 0x09 to start the SC's peripheral-test menu on its "
                         "FTDI UART (115200 8N1), then exit")
    ap.add_argument("--settle", type=float, default=2.0)
    a = ap.parse_args(argv)

    with sclink.ScLink() as link:
        sm = link.sysmon()
        if not sm:
            print("no reply from the bridge — is the bridge bitstream loaded?", file=sys.stderr)
            return 3
        print(f"SYSMON: VCCINT {sm['vccint_mv']} mV, VCCAUX {sm['vccaux_mv']} mV, "
              f"{sm['temp_c']:.1f} C")
        if a.show:
            return 0

        ses = sclink.ScSession(link)
        h = ses.handshake()
        if not h:
            print("satellite controller did not answer identify", file=sys.stderr)
            return 3
        fwdata = h["fw"][1] if h["fw"] else b""
        fw = a.fw or ("%d.%d" % (fwdata[2], fwdata[3]) if len(fwdata) >= 4 else "stock")
        print(f"SC: firmware {fw}, extended command set {'present' if h['trm_firmware'] else 'ABSENT'}")
        if not h["trm_firmware"]:
            print("this controller has no rail-setting command; a stock SC cannot do it",
                  file=sys.stderr)
            return 2
        if a.enable_menu:
            r = ses.talk(scvolt.start_menu(), wait=1.0)
            print("  sent: 0x09 start menu | reply:",
                  f"cmd=0x{r[0]:02x} data={r[1].hex()}" if r else "(none)")
            print("  The menu task is created once and then stays up, so a second")
            print("  0x09 is harmless and will look identical.")
            print("  Now open the controller's FTDI channel at 115200 8N1 and press")
            print("  Enter; the three UART channels are usually /dev/ttyUSB1..3 and")
            print("  only one of them answers.")
            return 0

        if fw not in scvolt.FLOOR_MV:
            print(f"unknown firmware {fw}; refusing rather than guessing a floor", file=sys.stderr)
            return 2

        mv = []
        for i, word in enumerate((a.vccint, a.vccbram, a.vccmem)):
            mv.append(scvolt.level(word, i, fw) if word is not None else None)
        if not any(x is not None for x in mv):
            return 0
        try:
            frame = scvolt.set_rails(mv[0], mv[1], mv[2], fw=fw)
        except ValueError as e:
            print("REFUSED:", e, file=sys.stderr)
            return 2

        for i, rail in enumerate(scvolt.RAIL_ORDER):
            if mv[i] is not None:
                print(f"  {scvolt.RAIL_NAME[rail]:<8} -> {mv[i]} mV")
        r = ses.talk(frame, wait=1.0)
        print("  sent:", frame.hex(), "| reply:",
              f"cmd=0x{r[0]:02x} data={r[1].hex()}" if r else "(none)")

        time.sleep(a.settle)
        after = link.sysmon()
        print(f"SYSMON after: VCCINT {after['vccint_mv']} mV ({sm['vccint_mv']} before)")
        if mv[0] is not None:
            off = after["vccint_mv"] - mv[0]
            if abs(off) <= 15:
                print(f"  CONFIRMED on-die, {off:+d} mV of the commanded value")
            else:
                print(f"  WARNING: on-die reads {off:+d} mV from the command", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

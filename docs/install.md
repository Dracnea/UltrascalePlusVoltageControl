# Installing upvc (no Vivado)

`upvc` sets a card's rails over its FTDI JTAG cable. It is pure Python (standard
library only) and talks to the cable through the system `libftdi1` with `ctypes`.
**Vivado is only needed to rebuild the bridge bitstreams** (`rtl/`, `boards/`);
the prebuilt bridges ship in every install below and are checked against
`rtl/prebuilt/SHA256SUMS` before each load. You never need Vivado to set rails.

## Board status on the Vivado-free path

| board | status |
|---|---|
| SQRL FK33 | hardware-proven without Vivado; re-test pending after the multi-SLR loader change |
| Varium C1100 / Alveo U55N | pending hardware test (2-SLR path in `host/ftdijtag.py`) |
| Alveo U55C | pending hardware test (3-SLR path in `host/ftdijtag.py`) |
| TUL TH53, TH55 | pending hardware test |

`upvc` maps part to bridge exactly as `changeVoltage.sh` does and runs the same
guard and request checks first; whether a given board's JTAG path works is the
"status" above, not a promise of the installer. The C1100/U55N and U55C also
need extended satellite-controller firmware and must be off the PCIe bus
(see [hardware.md](hardware.md)).

## 1. Debian / Ubuntu package

```console
$ packaging/deb/build-deb.sh dist           # writes dist/upvc_<version>_all.deb
$ sudo apt install ./dist/upvc_0.1.0_all.deb
```

Installs to `/opt/upvc`, `/usr/bin/upvc`, and the udev rule in
`/lib/udev/rules.d`. Depends on `python3 (>= 3.8)` and `libftdi1-2`.

## 2. pipx / pip

```console
$ sudo apt install libftdi1-2               # the system library
$ pipx install .                            # or: pip install .
```

The wheel carries `host/*.py`, `rtl/prebuilt/*.bit.xz`, `SHA256SUMS` and
`boards/u55c/*.bit.xz` as package data. They are not moved in the repository:
`setup.py` copies them into the package (`upvc/_host`, `upvc/_data`) at build
time, and `changeVoltage.sh` keeps using the originals.

## 3. From a checkout

```console
$ PYTHONPATH=packaging/python python3 -m upvc list
$ SCLINK_FTDI=<serial> ./changeVoltage.sh --vccint min     # the original script
```

From a checkout `upvc` finds `host/` and `rtl/prebuilt/` next to
`packaging/python`. `pip install -e .` works the same way.

## udev (Linux)

Without a rule only root can open the cable. `packaging/udev/99-upvc-ftdi.rules`
grants the `plugdev` group access to FTDI 0403:6010 and 0403:6011:

```console
$ sudo install -m 0644 packaging/udev/99-upvc-ftdi.rules /etc/udev/rules.d/
$ sudo udevadm control --reload-rules && sudo udevadm trigger
$ sudo usermod -aG plugdev "$USER"          # log out and in; replug the cable
```

The .deb installs the rule. It matches every device with those IDs, not only cards.

## Using it

```console
$ upvc list                                  # cables present (opens nothing)
$ upvc list --probe                          # also read each IDCODE (opens channel A if free)
$ upvc show --serial <serial>
$ upvc set  --serial <serial> --vccint 720 --vccbram min --vccmem min
$ upvc limits | upvc bits | upvc selftest    # no hardware
```

`--serial` is always required; `upvc` never chooses a cable. Only one process may
hold an FTDI channel: stop Vivado/hw_server first (SIGTERM, never SIGKILL).
Undervolting makes the fabric go mute until reprogrammed; overvolting destroys
the part, and the ceilings cannot be overridden. Rails are global and persist
across reconfiguration.

## Windows (untested)

Nothing here has been run on Windows. What it would need:

1. **libftdi1.dll.** Build libftdi1 (and libusb-1.0) for Windows or take a
   prebuilt copy, e.g. from the MSYS2 `mingw-w64-x86_64-libftdi` package.
   Put `libftdi1.dll` and `libusb-1.0.dll` in one directory.
2. **A WinUSB driver on interface A.** Run [Zadig](https://zadig.akeo.ie/), list
   all devices, select the cable's *interface 0* (channel A, "Dual RS232-HS" /
   "Quad RS232-HS"), and install WinUSB. This replaces the FTDI VCP driver on
   that interface, so Vivado's own FTDI driver will no longer see it until you
   restore it.
3. **Tell upvc where the DLL is:** `set UPVC_LIBFTDI=C:\path\to\libftdi1.dll`.
   `upvc` runs the host scripts through a small shim that points
   `ctypes.util.find_library("ftdi1")` at that file.
4. `upvc list` reads Linux sysfs and prints nothing useful on Windows; find the
   serial in Device Manager or Zadig and pass it as `--serial`. The process
   checks (`pgrep`, `lspci`) are skipped when those tools are absent.

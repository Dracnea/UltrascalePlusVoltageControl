# UltraScale+ Voltage Control

Set the VCCINT, VCCBRAM and VCCMEM rails on a **Xilinx Varium C1100 / Alveo
U55N** or an **Alveo U55C** from the host, and verify the result on the die — with no mining
software, no vendor miner binary, and no bitstream you have to obtain from
anyone.

```console
$ ./changeVoltage.sh --vccint 720 --vccbram min --vccmem min
loading the bridge ...
  PART xcu50_u55n
  BIT .../rtl/scbridge_c1100.bit
loaded (End of startup status: HIGH).
SYSMON: VCCINT 751 mV, VCCAUX 1807 mV, 29.4 C
SC: firmware 1.3, extended command set present
  VCCINT   -> 720 mV
  VCCBRAM  -> 700 mV
  VCCMEM   -> 1050 mV
  sent: 5c0270000910d00236bc02341a04a1025c03 | reply: cmd=0xfe data=70
SYSMON after: VCCINT 721 mV (751 before)
  CONFIRMED on-die, +1 mV of the commanded value
```

## Why this exists

On these cards the FPGA fabric has **no I2C route to the core regulator**. A
satellite controller — a TI ARM Cortex-M microcontroller on the board — owns
the rails, and it is
reachable only over a UART that lands on fabric pins. AMD documents the
architecture and calls the protocol proprietary; no published command sets a
rail.

The one tool that could do it was a closed-source cryptocurrency miner whose
last release was in 2024 and whose download host has been returning 502 for
years. It also insisted on fetching and loading its own mining bitstream before
it would talk to the controller, so setting a voltage meant running a miner and
hoping the script stopped it again.

This does the same job with a bridge bitstream of under 300 LUTs you build yourself, a
few hundred lines of stdlib Python, and no miner anywhere.

## What you need

- A C1100 / U55N or U55C whose satellite controller runs **extended firmware** that
  implements the rail-setting command. Stock firmware does not have it and will
  refuse — see [docs/hardware.md](docs/hardware.md). **This project does not
  distribute that firmware**, and does not flash anything.
- Vivado, for building the bridge and loading it over JTAG. The bitstream is
  not committed — it is a 56 MB binary that rebuilds from `rtl/` in minutes, and
  a prebuilt one may be attached to a release.
- Python 3, standard library only.

## Quickstart

```sh
cd rtl && vivado -mode batch -source build.tcl && cd ..   # once, ~5 minutes (C1100)
cd rtl && BOARD=u55c vivado -mode batch -source build.tcl && cd ..   # U55C
./changeVoltage.sh --limits                               # what the rails allow
./changeVoltage.sh --vccint 720                           # set the core rail
```

Each rail takes a millivolt number, `min` (the firmware's floor) or `default`
(the card's stock setpoint).

The card is identified from the part Vivado reports (`xcu50_u55n` or
`xcu280_u55c`), which picks the bridge bitstream and the JTAG IR length (12 on
the two-SLR U55N, 18 on the three-SLR U55C), and is cross-checked against the
board name the controller itself returns. On a host with several cards, set
`SCLINK_SERIAL` to the card's JTAG serial.

**Setting the memory rails to `min` is the point of taking all three.** A design
that instantiates no BRAM and no HBM has no reason to hold VCCBRAM at 850 mV or
VCCMEM at 1200 mV. A design that *does* use HBM will break if you drop VCCMEM,
so know what your bitstream contains.

### The controller's own menu

The satellite controller carries an interactive peripheral-test menu that is not
running until something starts it:

```sh
./changeVoltage.sh --enable-menu
#   then open the controller's FTDI channel -- usually one of /dev/ttyUSB1..3 --
#   at 115200 8N1 and press Enter
```

It offers **`Set VccInt`**, `Set VccIntBram` and `Set VccIntHbm`, each prompting
`Enter mV:`, plus a full regulator register dump, sensor and power readouts, fan
control, an EEPROM dump and `Get Board Info` (board name, revision, serial, MAC
IDs, UUID, part number, memory size).

`Set VccInt` there reaches the rail **with no bitstream and no JTAG in the
path** — a serial terminal is enough once the menu is up. What this repository
still gives you over the menu is the ability to start it, to script a change,
and to verify one against SYSMON rather than against the controller's own
telemetry.

## Safety

- **The risk is asymmetric.** Undervolting makes the fabric stop meeting timing
  and go quiet until it is reprogrammed; nothing is destroyed. Overvolting
  destroys the part. The ceiling here is not overridable.
- **The setpoint is global and persists across reconfiguration.** Whatever you
  load next inherits it. That is also what makes this useful: set the rail, then
  load your design.
- **The card must come off the PCIe bus first.** This programs over JTAG, and
  dropping the link under a bound driver is how you get bus errors. The script
  refuses to run until you do, and prints the commands.
- **Nothing here flashes firmware.** The controller command that enters the
  bootloader is not implemented, and will not be.

## How it fits together

```
host/scvolt.py       builds the controller frame, and verifies its own encoding
host/sclink.py       carries bytes over JTAG BSCAN through Vivado
host/scset.py        sets the rails and confirms the result with SYSMON
rtl/sc_uart.sv       the controller's UART, as a module you can reuse
rtl/scbridge_top.sv  sc_uart behind a JTAG register, plus SYSMONE4
rtl/c1100_qsfp_leds_off.xdc  the six QSFP LED pins, for any C1100 design
tools/qsfp_leds_eco.sh       QSFP LEDs off in an already-routed image
changeVoltage.sh     the wrapper -- validate, guard, load, set, confirm
```

### QSFP LEDs off

The bridge also drives the card's six QSFP LEDs off, including the one beside
the micro-USB socket. Left unused, `UNUSEDPIN PULLUP` lights them. The LEDs
follow whichever image is loaded, so to keep them dark your own design has to
drive them too: add three ports and `rtl/c1100_qsfp_leds_off.xdc`, or run
`tools/qsfp_leds_eco.sh` on an image that is already routed. It uses no LUTs and
cannot move timing. Findings, pins and process are in
[docs/qsfp-leds.md](docs/qsfp-leds.md).

### Setting rails from a design that is already running

`rtl/sc_uart.sv` is the reusable half. The top level above is a **probe**: load
it, set a rail, load what you actually wanted. That works because the setpoint
persists across reconfiguration — but it does mean a bitstream swap.

If you would rather not swap, instantiate `sc_uart` directly behind whatever
control channel your design already has. Its interface is one-cycle strobes —
`cfg` / `q` / `burst` / `clr` / `rx_pop` — and it costs about 280 LUT. Then the
running design sets its own rails and reads them back, with no swap and nothing
to reload.

`SYSMONE4` reads VCCINT **on the die**. It never consults the satellite
controller, so the confirmation is an independent measurement rather than the
controller agreeing with itself.

## Credit, and what is not here

The extended satellite-controller firmware that makes rail setting possible at
all is the work of the TeamRedMiner authors. This project reimplements only the
*host side* of talking to it, from a clean-room decode plus a USB capture of a
working transaction; it contains none of their code, none of their bitstreams
and none of their firmware, and it cannot install any of it.

If your card has stock firmware, this tool will tell you so and stop.

## Status

Working and verified on hardware. Every figure below is from the bridge in this
repository — no third-party bitstream was involved:

| commanded | SYSMON reads on-die | error |
|---|---|---|
| VCCINT 750 mV | 750 mV | 0 |
| VCCINT 720, VCCBRAM 700, VCCMEM 1050 | 720 mV | 0 |
| VCCINT 800, VCCBRAM 850, VCCMEM 1200 | 801 mV | +1 mV |

The bridge costs **282 LUT / 371 FF** after routing — 0.03% of an xcu55n — and closes timing
with 4.2 ns to spare at 100 MHz (4.6 ns with the QSFP LED ports, which add no
LUTs). All six QSFP LEDs confirmed dark on the card with the bridge loaded.

On an **Alveo U55C** running the same extended firmware, through the same
code path with the U55C bridge:

| request | on-die VCCINT | regulator `VOUT_CMD` / `OUT_VOLT` |
|---|---|---|
| VCCINT 780 (from 801) | 781 mV | 780 / 780, `STATUS` 0 |
| VCCINT 800 (from 781) | 800 mV | |

Tested on C1100s and one U55C. Reports from other cards and other UltraScale+ boards are
welcome; [docs/protocol.md](docs/protocol.md) documents enough to extend it.

## If you are reimplementing this

Three bugs cost the most time here, and all three look like "the controller is
dead" rather than like bugs:

1. **8N1 gets you nothing.** The line is 8E1. No parity returns garbage, odd
   parity returns silence.
2. **DRCK does not toggle in Update-DR.** It is a gated TCK that runs in
   Capture-DR and Shift-DR only, so a command register clocked by it on UPDATE
   never latches and every read comes back zero. Sample the shift register from
   your own clock domain instead.
3. **Do not sample at the middle of the start bit.** Wait half a bit to confirm
   the edge, then a full bit to reach the middle of bit 0. Sampling one bit
   early shifts the start bit in as data and every byte is garbage — which is
   indistinguishable from a wrong baud rate.

A fourth, less obvious: **queue the frame and burst it.** One JTAG round-trip
costs tens of milliseconds, so a byte-per-round-trip transmitter spreads a frame
over seconds and the controller times out between characters.

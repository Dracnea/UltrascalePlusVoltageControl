# Turning off the C1100's QSFP LEDs

The Varium C1100 / Alveo U55N has six LEDs on its bracket, three per QSFP28
cage, and on an idle or headless card they stay lit whatever the FPGA is doing.
This page covers why that happens, and how to turn them off in the bridge here,
in your own design, or in an image you have already routed.

## Findings

- **Six LEDs, and the FPGA drives all six.** AMD's C1100 board files list them
  as component `qsfp28_leds`: an activity LED and a green and a yellow link
  status LED for each cage. Corundum's public AU55N target
  (`fpga/mqnic/Alveo/fpga_25g/fpga_au55.xdc`) has the same pins.

  | port | pin | board-file name |
  |---|---|---|
  | `qsfp_led_act[0]` | BL13 | `QSFP28_0_ACTIVITY_LED` |
  | `qsfp_led_stat_g[0]` | BK11 | `QSFP28_0_LINK_STAT_LEDG` |
  | `qsfp_led_stat_y[0]` | BJ11 | `QSFP28_0_LINK_STAT_LEDY` |
  | `qsfp_led_act[1]` | BK14 | `QSFP28_1_ACTIVITY_LED` |
  | `qsfp_led_stat_g[1]` | BK15 | `QSFP28_1_LINK_STAT_LEDG` |
  | `qsfp_led_stat_y[1]` | BL12 | `QSFP28_1_LINK_STAT_LEDY` |

  All six are in bank 68 at LVCMOS18.

- **They are active-high.** Driving all six pins low turns all six LEDs off.
  This was confirmed by eye on a card, not just inferred from the reference
  design.

- **`UNUSEDPIN PULLUP` is what lights them.** A design that does not use these
  pins leaves them to the bitstream's unused-pin setting. That is usually
  `PULLUP`, which is enough to light an active-high LED. Most designs for this
  card are in that position, including the plain bridge before this change.

- **They are the only port hardware the fabric controls.** The QSFP modules'
  `LPMODE`, `RESET_L` and `MODSEL_L` lines belong to an I2C expander on the
  satellite controller's side, not to FPGA pins. With no module in either cage,
  the ports draw no module power, and Vivado already powers down unused
  transceivers. So "turning the ports off" from the fabric comes down to the
  LEDs.

- **The LED beside the micro-USB socket is one of the six.** From its position
  it looks like a USB activity light, but it goes dark with the other QSFP
  LEDs. No FPGA pin drives a USB LED.

- **The LED state follows the loaded image, not the card.** A rail setpoint
  survives reconfiguration but an LED does not. Loading this bridge darkens the
  LEDs only until the next image is loaded, and if that image leaves the pins
  unused they light again. To keep them dark, every image you load has to drive
  them.

- **It costs nothing.** The pins are driven by output buffers in the I/O
  column, fed from a constant, so they use no LUTs, no flip-flops and no logic
  area, and add no timing paths. Power drops slightly, since six lit LEDs go
  dark.

## 1. The bridge in this repository

`rtl/scbridge_top.sv` drives all six LEDs off, and `rtl/build.tcl` reads
`rtl/c1100_qsfp_leds_off.xdc` and refuses to write a bitstream unless each LED
port is on its pin. Nothing to do: `./changeVoltage.sh` loads it, and the LEDs
go dark while it is loaded.

Verified on a C1100: the bridge built at 282 LUT / 371 FF (the LED ports add
none) with WNS +4.593 ns, set VCCINT 770 / VCCBRAM 850 / VCCMEM 1050 with SYSMON
reading 771 mV on-die, and all six LEDs were dark while it was loaded.

## 2. Your own design

Add three 2-bit output ports tied to zero, and read the pin file:

```systemverilog
output wire [1:0] qsfp_led_act,
output wire [1:0] qsfp_led_stat_g,
output wire [1:0] qsfp_led_stat_y,
...
assign qsfp_led_act    = 2'b00;
assign qsfp_led_stat_g = 2'b00;
assign qsfp_led_stat_y = 2'b00;
```

```tcl
read_xdc path/to/rtl/c1100_qsfp_leds_off.xdc
```

Drive the pins explicitly rather than setting
`BITSTREAM.CONFIG.UNUSEDPIN PULLDOWN`: that would change every other unused
pin on the device as well.

## 3. An image that is already routed

If you have a routed checkpoint and do not want to re-implement it (a long
build, or an image whose timing you have already signed off), add the LED
drive as an ECO:

```sh
VIVADO=/path/to/vivado \
  tools/qsfp_leds_eco.sh design_routed.dcp design.bit design-ledsoff.bit
```

`tools/qsfp_leds_eco.tcl` opens the checkpoint, creates the six ports, drives
each through an OBUF from a GND cell, places the OBUFs on the LED pins' IOBs,
routes **only** those nets and writes the bitstream. Every existing cell keeps
its placement and every existing net its route. It stops before writing
anything if:

- any of the six pins is already used by the design,
- setup WNS or hold WHS moves at all from the checkpoint's value,
- any net is unrouted or conflicting, or
- DRC reports an error.

Then `tools/bitregs.py` lists every configuration-register write outside frame
data in the original and the new bitstream and requires them to be identical.
That covers `USR_ACCESS` stamps, `COR0`/`COR1`, compression and the `IDCODE` of
each SLR. So the new image can differ from the original only in its frame data.

**Watch for bitstream properties set after the checkpoint.** If your flow sets
properties such as `BITSTREAM.GENERAL.COMPRESS TRUE` after its last
`write_checkpoint`, the checkpoint does not carry them and the ECO'd image will
not have them either. Pass them in:

```sh
tools/qsfp_leds_eco.sh design_routed.dcp design.bit design-ledsoff.bit \
    BITSTREAM.GENERAL.COMPRESS=TRUE
```

The register comparison is what catches this: a missing compression setting
shows up as differing `CTL1`/`MASK` writes and a bitstream roughly 20% larger
than the original.

### Results on hardware

On a C1100, the ECO was applied to two unrelated application images: one
built around a single core that spans the die, the other a six-core design. In both, setup and hold slack
were unchanged to the picosecond, DRC was clean, and every configuration
register matched the original (30 and 28 writes). The first image was then
loaded and run under its normal workload: it configured with `DONE=1` and
`CRC_ERR=0`, its identification word read back correctly, it produced correct
results, and all six LEDs stayed dark.

## Checking by eye

With the card in view, load the image and look at the bracket. All six LEDs,
including the one next to the micro-USB socket, should be off. If any stays
lit, check that its port reached its pin (`report_io`) and that the design
actually drives it to 0.

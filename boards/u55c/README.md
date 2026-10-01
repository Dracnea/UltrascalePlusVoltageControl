# Alveo U55C parking and pin-check image

A 56-LUT image for the U55C (`xcu55c-fsvh2892-2L-e`) that:

- drives the **six QSFP LEDs off** (`../../rtl/c1100_qsfp_leds_off.xdc` - the
  U55C uses the C1100's pins);
- holds **`hbm_cattrip` (BE45) low**, without which the satellite controller
  reads an HBM over-temperature and powers the card off;
- counts the **100 MHz SYSCLK3 (BK43/BK44)** and exposes the count over JTAG
  through BSCANE2 **USER4**, as a 64-bit capture of `{gray(count)[31:0], 0x55C0B0A1}`.

It talks to nothing else, and has no connection to the satellite controller.
Use it to keep a card dark between jobs, or as a known-good pin and clock base
for a U55C port.

**What a bitstream can and cannot do to the QSFP ports.** On this board only the
six LEDs and the GTY lanes reach the fabric. The cages' ResetL, LPMode and
ModSelL belong to the satellite controller. The GTYs are not instantiated here,
so Vivado powers them down. The LEDs are the only QSFP hardware an image
controls.

```sh
vivado -mode batch -source build.tcl                                # ~5 min -> u55c-board-ledsoff.bit
vivado -mode batch -source load_and_check.tcl -tclargs <serial>     # card OFF PCIe first
```

`load_and_check.tcl` unpacks the prebuilt `u55c-board-ledsoff.bit.xz` if there
is no local build. USER4 is `23924` in the 18-bit IR (USER4 `0x23` in the
master SLR's field, BYPASS `0x24` in the other two).

## Verified on a U55C, 2026-10-01

DONE=1, "End of startup status: HIGH". USER4 returned the marker `55c0b0a1`.
Two reads 2002 ms apart counted 200,235,787 cycles: **100.018 MHz**, so the
clock pins are right. Prebuilt image md5 (uncompressed)
`e26cd3a9b35d0d736dce754ea6f7016b`.

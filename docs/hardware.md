# Hardware notes — Varium C1100 / Alveo U55N

## The rails, and which way is dangerous

**The risk is asymmetric, and it decides which direction is safe to explore.**

- **Too low** — the fabric stops meeting timing and goes quiet, including
  whatever channel would have raised the rail again. Recovered by reprogramming,
  or a power cycle if it is truly mute. **Nothing is destroyed.**
- **Too high** — the part is damaged. Unrecoverable.

So undervolting is the direction to experiment in, and the ceiling in this tool
is not overridable.

The setpoint is **global and persists across reconfiguration**. That is what
makes the tool useful — set the rail, then load your design — and it is also the
trap: whatever you load next inherits it, including someone else's bitstream.

## Stock setpoints

| rail | stock |
|---|---|
| VCCINT | 800 mV |
| VCCBRAM | 850 mV |
| VCCMEM | 1200 mV |

A design that instantiates **no BRAM and no HBM** has no reason to hold the
memory rails up. Check your utilisation report before dropping VCCMEM: a design
that does use HBM will fail if you do.

## Power and cooling

The card's **75 W rating is a passive-cooling design point, not a delivery
ceiling.** With both the PCIe slot and the 8-pin AUX connector connected there
is considerably more available, and real designs draw well past 75 W.

Both must be connected. The card is single-slot and passively cooled, and it is
genuinely hard to keep cool in a desktop chassis — a blower attached to the back
is the usual answer. Watch temperatures before chasing clock.

## `hbm_cattrip`

**`hbm_cattrip` (BE45) must be driven low by any design that does not
instantiate HBM.** Left floating it reads as an HBM catastrophic-temperature
trip and the satellite controller powers the card off — which looks exactly like
a dead board. The bridge here drives it low.

## PCIe and JTAG

Programming over JTAG while the card is enumerated on PCIe **drops the link**,
and doing that under a bound driver is how you get bus errors. Take the device
off the bus first:

```sh
echo 1 | sudo tee /sys/bus/pci/devices/<addr>/remove
#   ... load a bitstream, set the rails ...
echo 1 | sudo tee /sys/bus/pci/rescan
```

`changeVoltage.sh` refuses to run until you have, and prints both commands.
Those two are the only steps that need root; loading and setting do not.

Only one process may hold an FTDI channel at a time. If something else has it —
another JTAG tool, a miner, an open Vivado hardware target — stop it with
**SIGTERM, never SIGKILL**: a hard kill leaves all four channels marked open at
the driver level with nothing holding them, and recovery needs a USB replug.

## Pin references

Pin assignments trace to Corundum's public Alveo targets
([github.com/corundum/corundum](https://github.com/corundum/corundum),
`fpga/mqnic/Alveo`), which is where this card family's satellite-controller UART
and GPIO assignments are published. They have been verified through
place-and-route and on hardware here.

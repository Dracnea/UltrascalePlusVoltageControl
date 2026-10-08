# TUL TH53 and TH55

The TH53 (xcvu33p) and TH55 (xcvu35p) are sister boards. Unlike the
C1100/U55C, their VCCINT regulator is **not behind a satellite controller**: it
is a PMBus device on an I2C bus wired straight to FPGA fabric pins. So the
bridge for these boards, `vrmbridge`, is a small I2C master rather than a UART,
and the host side speaks PMBus to the regulator directly.

```console
$ ./changeVoltage.sh --vccint 750
identifying the card ...
  PART xcvu35p
request: VCCINT -> 750 mV (the regulator's own limits are checked on the card)
loading the bridge ...
...
regulator 0x60: PMBus rev 0x33, VOUT_MODE 0x.., WRITE_PROTECT 0x00, STATUS_WORD 0x....
  VOUT_COMMAND 720 mV, READ_VOUT 719 mV  (SYSMON 718 mV)
  encoding confirmed: READ_VOUT 719 mV vs SYSMON 718 mV (+1)
  VCCINT 720 -> 750 mV in steps of at most 20 mV
    740 mV  (READ_VOUT 739)
    750 mV  (READ_VOUT 749)
SYSMON after: VCCINT 749 mV (718 before), READ_VOUT 749 mV
  CONFIRMED on-die, -1 mV of the commanded value
```

(The transcript shows the shape of a run; figures vary per board.)

## What is known about the boards

All of this was measured on the boards themselves: a clock census, a pull-up
sweep of bank 67, then an I2C scan of each pulled-up pair.

| | TH53 | TH55 |
|---|---|---|
| Part (Vivado hardware) | `xcvu33p_CIV` | `xcvu35p` |
| Part (build) | `xcvu33p_CIV-fsvh2104-2L-e` | `xcvu35p-fsvh2104-2LV-e` |
| 100 MHz clock | BB18/BC18, LVDS, `DIFF_TERM_ADV TERM_100` | same |
| VCCINT regulator | Infineon PMBus, 7-bit `0x60` | same |
| PMBus pins | L12 (SCL) / L13 (SDA), bank 67, LVCMOS18, external pull-ups | same |
| Shipped VCCINT | ~699 mV | ~718 mV |
| JTAG | on-board FT4232H, seen by Vivado's own hw_server | same |

The 100 MHz pair is easy to get wrong: **F13/F12 is dead on these boards**, and
the oscillator is only seen with both termination properties set.

On the regulator:

- **`VOUT_COMMAND` (0x21) and `READ_VOUT` (0x8B) are plain millivolts**, not
  PMBus LINEAR16: 720 means 0.720 V. Measured: writing 720, 750 and 800 moved
  `READ_VOUT` to 719, 749 and 799, and SYSMON agreed.
- `WRITE_PROTECT` (0x10) reads 0x00, so writes are accepted.
- Its own envelope, as read from a TH55's regulator: `VOUT_MAX` 950 mV, OV
  fault 977 mV, margin-high 875 mV, UV fault 425 mV. The TH53's has not been
  read out; the tool reads it on every run either way.
- The setpoint persists across FPGA reconfiguration, which is what makes a
  probe-then-load sequence work.
- Temperature (`READ_TEMPERATURE_1`, 0x8D) and current (`READ_IOUT`, 0x8C) are
  LINEAR11. The current figure's scale has not been checked against a meter.
- Only VCCINT is on this bus. There is no VCCBRAM or HBM-rail control here.

## How the tool protects the part

Every one of these is in `host/vrmset.py`, not in the bitstream:

1. **It proves the encoding before it writes.** `READ_VOUT` has to agree with
   SYSMON's on-die VCCINT within 25 mV, and `VOUT_COMMAND` with `READ_VOUT`.
   If they do not, either this is not the VCCINT regulator or its encoding is
   not millivolts, and nothing is written.
2. **Ceiling 876 mV**, DS923's recommended maximum for VCCINT on parts run at
   0.85 V. It is not overridable, and the regulator's own `VOUT_MAX` (−25 mV)
   and OV fault limit (−50 mV) lower it further when they are lower.
3. **Floor 600 mV**, raised to clear the regulator's UV fault (+75 mV) and
   power-good thresholds (+25 mV) when it reports them. Undervolting does not
   damage the part, but tripping UV or power-good can switch the rail **off**,
   and on these boards that is a power cycle, not a reprogram.
4. **Steps of at most 20 mV**, each written, read back and checked against
   `STATUS_WORD`. A fault mid-ramp puts the rail back where it started.
5. It never touches `WRITE_PROTECT` and refuses if it is set, and it refuses to
   move a rail that already reports a fault.

## Osprey E3xx boards are refused

The Osprey E300 family (E309, E333, E335 and their C variants) uses the same
dies as these boards. A plain E335 module even reports the TH55's IDCODE
(`0x14b71093`), so the part alone cannot tell them apart. They are different
boards in every way that matters here: their rails belong to the box's own
controller, not to anything on the FPGA's pins, and loading any bitstream onto
a module replaces the design it was running.

`host/boardguard.py` refuses them **before the bridge is loaded and before any
PMBus traffic**. Any one signal is enough, and there is no override:

| check | when | an E3xx shows | a TH53 / TH55 shows |
|---|---|---|---|
| die variant | after the read-only identify | plain `xcvu33p` (E333) or `xcvu35p_CIV` (E335C) | `xcvu33p_CIV` (TH53), plain `xcvu35p` (TH55) |
| XVC `getinfo:` | before Vivado opens an XVC target | `xvcServer_v1.0:2048`, on ports 2541-2543 | a different server |
| the host behind XVC | same | controller API on :8200 (`/controller/getAllInfo`), Osprey page on :80 | neither |

A local USB cable gets only the die check: a TH board's JTAG is its on-board
FT4232H, and an E3xx module's JTAG is not wired to the host at all.

The die rule leans on the TH55s measured so far being plain VU35P and
the TH53s being CIV. A TH55 on a CIV die would be refused. That is the safe
way to be wrong, and the fix is to add its IDCODE here once one is seen.

Checked against a real E335: the controller-API signature identifies it and
the guard refuses it (`boardguard.py --selftest` covers every row with fake
endpoints). An E3xx's rails are set through its own controller.

## Using it through a remote JTAG server

If the card's JTAG is served over Xilinx Virtual Cable (for example by another
program that already owns the USB device), point the tool at it:

```sh
SCLINK_XVC=192.0.2.10:2542 ./changeVoltage.sh --vccint 750
```

## Setting the rail from inside your own design

`rtl/pmbus_i2c.sv` is the reusable half: one-cycle `go`, a mode, an address, a
register and a 16-bit word in; read data and per-byte ACKs out. Clock
stretching is honoured, and a bus held low aborts after 25 ms rather than
hanging. It is simulated in `sim/` against a behavioural regulator, with and
without clock stretching (`sim/run.sh`).

## Other rails

VCCINT is the only rail this tool sets on these boards. Whether any other can
be set depends on what the fabric's PMBus reaches, and that has not been
mapped past VCCINT. What is known: on L12/L13 only `0x60` and `0x0C` answer,
and `0x0C` is the SMBus Alert Response Address, so it is the same regulator
signalling an alert rather than a second device. The board's two other
pulled-up pin pairs in bank 67 (E10/E11, A14/B14) have nothing on them. So if
another rail is reachable at all, it is a second output of the `0x60`
controller, on another PMBus page. Multi-output controllers commonly carry one,
but which rail it powers (if any) is board-specific.

The process, in order, and nothing in step 2 or 3 happens until step 1 says so:

**1. Survey the bus (read-only, about a minute).**

```sh
./changeVoltage.sh --survey --out th53-survey.json
```

This scans every address with RECEIVE_BYTE (no command byte sent), reads the
regulator's identity (MFR_ID, MFR_MODEL, IC_DEVICE_ID; block reads), and walks
PAGE 0-3. For every page it reads the setpoint, measured output, limits,
current, temperature and status, then names the page by matching its measured
output against SYSMON's own on-die VCCINT, VCCBRAM and VCCAUX. PAGE is the only
register written, and it is put back to 0. Send back the JSON file.

**2. Decide per rail, from the survey.** A rail can only be offered for setting
if its page is *identified by SYSMON* — the same independent witness VCCINT
gets — and only within the DS923 recommended operating range for that rail.

| rail | SYSMON witness | DS923 recommended range | verdict |
|---|---|---|---|
| VCCBRAM (shared with VCCINT_IO) | yes | 0.825-0.876 V | settable if a page is identified as it; ~50 mV of room, so the gain is small |
| VCC_HBM | no | 1.164-1.236 V | not settable: 1.2 V is also MGTAVTT, and nothing on the die can tell them apart |
| MGTAVCC, MGTAVTT, VCCAUX_HBM | no | — | not settable, same reason |
| VCCAUX / VCCAUX_IO | yes | 1.746-1.854 V | never: configuration and I/O depend on it, and there is nothing to gain |

A rail SYSMON cannot see could become settable only with an outside
identification — the board schematic, or a meter on the rail while its page's
setpoint is nudged — and that would be a separate, deliberate change to this
document first.

**3. Build it, if step 2 allows.** No new bitstream: the bridge already reaches
every page of `0x60` and already reports VCCBRAM. The change is host-side: a
`--vccbram` option on these boards that selects the identified page, applies
that rail's limits, ramps in small steps with readback like VCCINT, and
confirms against SYSMON's VCCBRAM.

## Status

- **Confirmed on hardware** on both a TH53 and a TH55.
- **Built:** `vrmbridge_th53.bit` and `vrmbridge_th55.bit` route and meet
  timing at 100 MHz.
- **Simulated:** the I2C master, against a PMBus slave model: every transaction
  shape (including receive-byte and block reads), pages, NACK from an absent
  address, clock stretching, and a stuck bus (`sim/run.sh`).
- **Host logic tested** against fake bridges (`vrmset.py --selftest`): ramp
  steps, every refusal path, and the survey's paging and identification.
- **`--survey` is new and has not yet been run on a board.**

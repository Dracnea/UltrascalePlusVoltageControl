# The satellite-controller protocol

The C1100 / U55N carries a TI satellite controller that owns the board's
voltage regulators. AMD documents the architecture — an external MCU talking to
firmware in the FPGA "through the UART using a proprietary protocol" — and
publishes neither the frame format nor any command that sets a rail. This is
what the protocol turns out to be.

The controller is an **ARM Cortex-M**, not the MSP430 the older Alveo tooling
implies. Its firmware's vector table has an initial stack pointer of
`0x2003B000`, so the part carries at least 236 KB of SRAM and cannot be the
64 KB MSP432P401R used on the XBB / VCU1525 boards.

Decoded from a miner binary that implements it, then **confirmed byte for byte
against a USB capture of a working transaction**, then **spoken directly** from
the bridge in this repository, and finally **checked against the controller's
own firmware image** — which is where the limits and the menu below come from.

## The link

| | |
|---|---|
| transmit (FPGA → controller) | **BH42** |
| listen (controller → FPGA) | **BJ42** |
| framing | **115200 8E1** |

Even parity is not optional: **8N1 returns garbage and 8O1 returns silence.**
This is the single most expensive detail here — the controller looks dead if you
speak 8N1 to it, and that is how most attempts at this end.

It also **never speaks first.** With the lines idle the edge counters do not
advance at all, so silence on an unprompted line is not evidence of anything.

## Frame format

```
5C 02 | cmd | 00 | len | data[len] | cksum_lo cksum_hi | 5C 03
        \__________ escaped region ___________________/
```

- `5C 02` opens, `5C 03` closes.
- Any `0x5C` inside the body is **doubled**.
- Checksum is a plain **16-bit sum of `cmd + 00 + len + data`**, little-endian.
- Byte 1 is `0x00` in every observed frame, in both directions. Role unknown.

## Commands

| cmd | meaning | reply |
|---|---|---|
| `0x03` | identify / open session | `0x83` |
| `0x04` | negotiate protocol version, one data byte `min(ver, 9)` | `0xFE` |
| `0x05` | read all sensors | `0x85` |
| `0x70` | **set voltages** | `0xFE` |
| `0x71` | query extended firmware version | `0x72` |
| `0x06` `0x07` `0x08` | monitor polls | |
| `0x09` | **start the peripheral-test menu task** | `0x89` |
| `0xFF` | NAK — what a stock controller returns to everything | |

This table used to be a reading of the host binary. It is now a reading of the
**controller firmware**: the dispatcher decodes commands in three pieces — a
`tbh` jump table covering `0x01`–`0x0D`, a compare chain for `0x14`, `0x15`,
`0x18`, `0x20`, `0x29`, `0x2B`, `0x2C`, and a second chain for `0x2D`, `0x31`,
`0x32`, `0x6F`, `0x70`, `0x71`. Everything else falls through to *"Received
unknown command, msg ID is : %x"*. So the accepted set is now closed, even
though most of those ids have not been traced to a behaviour.

A session, exactly as it runs:

```
identify      5c 02 03 00 00 03 00 5c 03   ->  0x83  data 01 09
comm version  5c 02 04 00 01 09 0e 00 5c 03 -> 0xFE  data 04
firmware      5c 02 71 00 00 71 00 5c 03   ->  0x72  data 00 01 01 03
set rails     5c 02 70 00 09 ...            ->  0xFE  data 70
```

**`0x72` answering at all is the flag that gates rail setting.** Stock firmware
never answers `0x71`, so the flag stays clear and the rail commands are refused.
The `01 03` inside the reply is the firmware version — 1.3 — which is what
decides the floors below.

## Setting rails

`cmd = 0x70`, payload is one to three 3-byte records:

```
[rail_id] [mV_lo] [mV_hi]        millivolts, uint16 little-endian
```

| rail_id | rail |
|---|---|
| `0x10` | VCCINT |
| `0x36` | VCCBRAM |
| `0x34` | VCCMEM / VCCHBM |

**A one-record write is safe**, which earlier versions of this document warned
against on the grounds that nobody had tried it. The handler is now known: it
rejects any payload whose length is not a multiple of three, walks the records
once to check every rail id is `0x10`, `0x34` or `0x36` — refusing the entire
frame without writing anything if one is not — and only then applies them in a
second pass. It loops over however many records it was given. Rails left out
are simply not touched.

The reference implementation does always carry all three, but that is its habit
rather than a requirement.

The one real caveat is that **there is no rollback.** Records are applied in
payload order, so a frame whose third record is refused leaves the first two
live. Put VCCINT first, as the reference implementation does, so the rail that
matters is the one that lands.

Worked frame, VCCINT 720 / VCCBRAM 700 / VCCMEM 1050:

```
5c 02 70 00 09 | 10 d0 02 | 36 bc 02 | 34 1a 04 | a1 02 | 5c 03
```

## Floors and ceilings

The firmware version gates how low each rail may go:

| firmware | VCCINT | VCCBRAM | VCCMEM |
|---|---|---|---|
| 1.3 | **500 mV** | 700 mV | 1050 mV |
| 1.2 | 625 mV | 700 mV | 1050 mV |
| stock | 675 mV | 700 mV | 1100 mV |

Read the version with `0x71` rather than assuming; `scset.py` does.

For firmware **1.3** these are no longer inferred. Each rail has its own setter
in the controller firmware, and each begins with a literal range check:

| rail | accepts | setter | PMBus |
|---|---|---|---|
| VCCINT | `0x1F4`–`0x3B6` = **500–950 mV** | `0xE920` | page 0 |
| VCCBRAM | `0x2BC`–`0x3B6` = **700–950 mV** | `0xE93C` | page 1 |
| VCCMEM / HBM | `0x41A`–`0x546` = **1050–1350 mV** | `0xE958` | second regulator, page 0 |

Out of range, the setter **returns failure without writing** — it does not
clamp to the nearest legal value, so a rejected write leaves the rail exactly
where it was. In range, it writes PMBus `VOUT_COMMAND` (register `0x21`) as a
16-bit little-endian count of **millivolts**, one LSB per mV, to an
**ISL68124**. That one-to-one mapping is why a commanded 720 mV reads back as
721 mV on the die rather than landing on some quantised step.

The 1.2 and stock rows have never been read out of an image and remain
inherited assumptions.

`scvolt.py` keeps the tool's own VCCINT ceiling at **900 mV**, 50 mV below what
the firmware would accept. Undervolting only makes the fabric go mute until it
is reprogrammed; overvolting destroys the part, and nothing this tool exists for
needs VCCINT above 900 mV.

## The controller's own menu

`0x09` starts a task inside the controller that puts an interactive
peripheral-test menu on the FTDI UART channel wired to it, at **115200 8N1**.
The handler creates the task with a 1 KiB stack behind a once-only flag, so
sending `0x09` twice is harmless and looks identical.

It is not running by default. With the card idle and nothing holding any FTDI
channel, all three UART channels are silent both passively and after a bare
carriage return; the task has to be started first.

```sh
./changeVoltage.sh --enable-menu
#   then open the controller's channel -- usually one of /dev/ttyUSB1..3 --
#   at 115200 8N1 and press Enter
```

The menu is numbered, printed as `%d %s` with `(q) quit`, and includes
**`Set VccInt`**, `Set VccIntBram` and `Set VccIntHbm`, each prompting
`Enter mV:`; a full ISL68124 register dump showing `VOUT_COMMAND`, `VOUT_MIN`,
`VOUT_MAX`, `VOUT_TRIM`, the margins and the three temperatures; voltage,
current and total-power readouts; fan control; EEPROM dump; and `Get Board
Info`, which prints the board name, revision, serial, MAC IDs, UUID, part
number, OEM ID, memory size, manufacturing date and the BSL version fields.

The significance is that **`Set VccInt` reaches the rail with no bitstream, no
JTAG and no BSCAN bridge in the path** — everything this repository builds
exists only because the rail write had to travel through fabric. Once the menu
is up, a serial terminal is enough. The bridge is still needed to *start* it,
since `0x09` travels over the same link as everything else, and it remains the
only way to script a rail change or to verify one with SYSMON.

## Verification

The bridge in this repository also instantiates `SYSMONE4`, which reads VCCINT
**on the die**. It does not consult the satellite controller, so it is an
independent witness rather than the controller agreeing with itself:

| commanded | SYSMON reads | error |
|---|---|---|
| 750 mV | 750 mV | 0 |
| 720 mV | 721 mV | +1 mV |
| 800 mV | 801 mV | +1 mV |

Trust that over any telemetry the controller reports about itself.

## Not implemented, deliberately

`0x01` enters the TI BSL to flash firmware. It is not implemented in
this repository and will not be. Flashing permanently replaces vendor firmware,
and an interruption leaves the board unable to power up until its controller is
reprogrammed over a JTAG debug header. That is an operator decision made with
the card in front of you, not something a voltage tool should be able to do.

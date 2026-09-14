# The satellite-controller protocol

The C1100 / U55N carries an MSP430 satellite controller that owns the board's
voltage regulators. AMD documents the architecture — an external MCU talking to
firmware in the FPGA "through the UART using a proprietary protocol" — and
publishes neither the frame format nor any command that sets a rail. This is
what the protocol turns out to be.

Decoded from a miner binary that implements it, then **confirmed byte for byte
against a USB capture of a working transaction**, then **spoken directly** from
the bridge in this repository.

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
| `0xFF` | NAK — what a stock controller returns to everything | |

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

**Send all three.** The reference implementation never writes a lone VCCINT
record — it always carries the other two at their current values. A one-record
write is something no known implementation has done and nobody has tested.

Worked frame, VCCINT 720 / VCCBRAM 700 / VCCMEM 1050:

```
5c 02 70 00 09 | 10 d0 02 | 36 bc 02 | 34 1a 04 | a1 02 | 5c 03
```

## Floors

The firmware version gates how low each rail may go:

| firmware | VCCINT | VCCBRAM | VCCMEM |
|---|---|---|---|
| 1.3 | **500 mV** | 700 mV | 1050 mV |
| 1.2 | 625 mV | 700 mV | 1050 mV |
| stock | 675 mV | 700 mV | 1100 mV |

Read the version with `0x71` rather than assuming; `scset.py` does.

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

`0x01` enters the MSP430 bootloader to flash firmware. It is not implemented in
this repository and will not be. Flashing permanently replaces vendor firmware,
and an interruption leaves the board unable to power up until its controller is
reprogrammed over a JTAG debug header. That is an operator decision made with
the card in front of you, not something a voltage tool should be able to do.

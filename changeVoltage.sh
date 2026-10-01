#!/usr/bin/env bash
# changeVoltage — set a Varium C1100 / U55N or Alveo U55C's rails without TeamRedMiner.
#
#   ./changeVoltage.sh --vccint 700                       core only
#   ./changeVoltage.sh --vccint 700 --vccbram min --vccmem min
#   ./changeVoltage.sh --limits                           what each rail allows
#   ./changeVoltage.sh --enable-menu                      start the SC's own menu
#
# Each rail takes a millivolt number, `min` (the SC firmware's floor) or
# `default` (the card's stock setpoint). Setting the memory rails to `min` is
# the point of taking all three: a design with no BRAM and no HBM has no reason
# to hold VCCBRAM at 850 mV or VCCMEM at 1200 mV, and a compute-bound design
# that instantiates 0 BRAM tiles, 0 URAM and no HBM is exactly that shape.
#
# The card is identified from the part Vivado reports, which picks the bridge
# (rtl/scbridge_c1100.bit or rtl/scbridge_u55c.bit; SCVOLT_BIT overrides). On a
# host with more than one card, SCLINK_SERIAL picks the JTAG target.
#
# It loads a small bitstream of ours (scbridge, ~300 LUT) purely to get a UART
# onto the satellite controller's pins, then speaks the protocol in scvolt.py.
# TeamRedMiner is not involved and no .bxz is needed.
#
# SAFETY
#   - Undervolting makes the fabric go mute until it is reprogrammed. Nothing is
#     destroyed. OVERVOLTING DESTROYS THE PART, so the ceiling is not overridable.
#   - Setting VCCMEM to min will break any design that actually uses HBM. Know
#     what your bitstream instantiates before you drop it.
#   - The rail is GLOBAL and PERSISTS across reconfiguration. Whatever you load
#     next inherits it.
#   - This never flashes satellite-controller firmware.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIT="${SCVOLT_BIT:-}"          # empty: chosen from the device's part below
# The card's PCIe address, if it is enumerated. Auto-detected when there is
# exactly one Xilinx device; set SCVOLT_PCI on a host with several.
PCI="${SCVOLT_PCI:-$(lspci -Dn -d 10ee: 2>/dev/null | awk 'NR==1{print $1}')}"
VIVADO="${VIVADO:-$HOME/Xilinx/2026.1/Vivado/bin/vivado}"

die() { echo "changeVoltage: $*" >&2; exit 1; }

[ $# -gt 0 ] || { python3 "$HERE/host/scvolt.py" --limits; echo; die "give at least one rail"; }

# --- 1. validate the request first: no hardware is touched if it is bad
#
# --enable-menu builds no rail frame -- it sends 0x09, which starts the
# controller's own peripheral-test menu on its FTDI UART -- so it skips the
# frame check but still takes the bridge path below, because 0x09 travels over
# the same link as everything else.
case "$*" in
    *--enable-menu*)
        case "$*" in
            *--vccint*|*--vccbram*|*--vccmem*)
                die "--enable-menu sets no rails; run it on its own" ;;
        esac
        ;;
    *)
        FRAME_OUT="$(python3 "$HERE/host/scvolt.py" "$@")" || { echo "$FRAME_OUT" >&2; exit 2; }
        case "$*" in *--limits*|*--selfcheck*) echo "$FRAME_OUT"; exit 0 ;; esac
        grep -q '^frame: ' <<<"$FRAME_OUT" || { echo "$FRAME_OUT" >&2; die "no frame built"; }
        ;;
esac

# --- 2. the card has to be off the PCIe bus: this programs over JTAG, and
#        dropping the link under a bound driver is how you get bus errors
if [ -n "$PCI" ] && [ -d "/sys/bus/pci/devices/$PCI" ]; then
    cat >&2 <<EOF
changeVoltage: $PCI is still on the PCIe bus.

  Programming over JTAG drops the link. Take it off first:
      echo 1 | sudo tee /sys/bus/pci/devices/$PCI/remove
  and put it back when you are done:
      echo 1 | sudo tee /sys/bus/pci/rescan
EOF
    exit 3
fi

# --- 3. only one process may hold an FTDI channel
#
# -x matches the process NAME, not the command line. `pgrep -f` here matched the
# shell that was *running this script* whenever the invoking command happened to
# mention one of these names, which is a self-match, and it cost a bring-up run.
# hw_server is deliberately NOT in this list. Vivado launches one and leaves it
# running between sessions, and the next Vivado reconnects to it rather than
# fighting it -- it only claims the FTDI while a target is open. Treating a
# stale hw_server as a blocker makes the tool refuse to run after its own
# previous success.
holder=""
for n in changeVoltage openFPGALoader xsdb; do
    pids=$(pgrep -x "$n" 2>/dev/null | grep -v "^$$\$" || true)
    [ -n "$pids" ] && holder="$holder $n($pids)"
done
if [ -n "$holder" ]; then
    echo "changeVoltage: JTAG channel held by:$holder" >&2
    die "stop it with SIGTERM, never SIGKILL — a hard kill leaves all four FTDI channels marked open"
fi

# --- 4. load the bridge bitstream
#
# The bridge is per part: a C1100 image will not load on a U55C and vice versa,
# so the Tcl reads PART off the device and picks the matching file, and refuses
# a part it does not know rather than guessing.
[ -x "$VIVADO" ] || die "vivado not found at $VIVADO"
[ -z "$BIT" ] || [ -f "$BIT" ] || die "bitstream not found: $BIT"
# Prebuilt bridges ship xz-compressed in rtl/prebuilt/ (~50 KB against 56-85 MB
# raw); unpack any that has not been built or unpacked locally yet.
for xzf in "$HERE"/rtl/prebuilt/scbridge_*.bit.xz; do
    [ -f "$xzf" ] || continue
    raw="$HERE/rtl/$(basename "${xzf%.xz}")"
    [ -f "$raw" ] || { echo "unpacking $(basename "$xzf")"; xz -dc "$xzf" > "$raw"; }
done
SERIAL="${SCLINK_SERIAL:-}"
# Vivado must source a FILE. `-source /dev/stdin` with a heredoc silently does
# nothing: vivado reads stdin itself, runs no script, and exits in three
# seconds -- which looked exactly like a successful load and cost a bring-up.
tcl="$(mktemp -t scvolt-load-XXXXXX.tcl)"
log="$(mktemp -t scvolt-load-XXXXXX.log)"
trap 'rm -f "$tcl" "$log"' EXIT
cat > "$tcl" <<TCL
open_hw_manager
connect_hw_server -allow_non_jtag
if {"$SERIAL" ne ""} {
    set t [lsearch -inline -glob [get_hw_targets] *$SERIAL*]
    if {\$t eq ""} { error "no hw_target matching $SERIAL" }
} else {
    if {[llength [get_hw_targets]] != 1} { error "more than one hw_target -- set SCLINK_SERIAL" }
    set t [lindex [get_hw_targets] 0]
}
open_hw_target \$t
set d [lindex [get_hw_devices] 0]
current_hw_device \$d
set part [get_property PART \$d]
set bit {$BIT}
if {\$bit eq ""} {
    switch -glob -- \$part {
        *u55c*  { set bit {$HERE/rtl/scbridge_u55c.bit} }
        *u55n*  { set bit {$HERE/rtl/scbridge_c1100.bit} }
        default { error "unsupported part \$part -- set SCVOLT_BIT to a bridge built for it" }
    }
}
if {![file exists \$bit]} { error "bitstream not found: \$bit (build it: cd rtl && BOARD=... vivado -mode batch -source build.tcl)" }
puts "PART \$part"
puts "BIT \$bit"
set_property PROGRAM.FILE \$bit \$d
program_hw_devices \$d
puts "PROGRAM_OK"
close_hw_target
TCL
echo "loading the bridge ..."
"$VIVADO" -mode batch -nojournal -nolog -source "$tcl" > "$log" 2>&1 || true
grep -E "^(PART|BIT) " "$log" | sed 's/^/  /' || true
if ! grep -q PROGRAM_OK "$log"; then
    echo "--- vivado ---" >&2; tail -20 "$log" >&2
    die "programming failed (no PROGRAM_OK)"
fi
grep -qi "End of startup status: HIGH" "$log" \
    && echo "loaded (End of startup status: HIGH)." \
    || echo "loaded (vivado reported no startup status -- verify before trusting it)."

# --- 5. speak to the satellite controller
exec python3 "$HERE/host/scset.py" "$@"

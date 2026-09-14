#!/usr/bin/env bash
# changeVoltage — set the C1100's rails without TeamRedMiner.
#
#   ./changeVoltage.sh --vccint 700                       core only
#   ./changeVoltage.sh --vccint 700 --vccbram min --vccmem min
#   ./changeVoltage.sh --limits                           what each rail allows
#
# Each rail takes a millivolt number, `min` (the SC firmware's floor) or
# `default` (the card's stock setpoint). Setting the memory rails to `min` is
# the point of taking all three: a design with no BRAM and no HBM has no reason
# to hold VCCBRAM at 850 mV or VCCMEM at 1200 mV. The BLAKE3 farm is exactly
# that shape — 0 BRAM tiles, 0 URAM, no HBM instantiated.
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
BIT="${SCVOLT_BIT:-$HERE/rtl/scbridge_c1100.bit}"
# The card's PCIe address, if it is enumerated. Auto-detected when there is
# exactly one Xilinx device; set SCVOLT_PCI on a host with several.
PCI="${SCVOLT_PCI:-$(lspci -Dn -d 10ee: 2>/dev/null | awk 'NR==1{print $1}')}"
VIVADO="${VIVADO:-$HOME/Xilinx/2026.1/Vivado/bin/vivado}"

die() { echo "changeVoltage: $*" >&2; exit 1; }

[ $# -gt 0 ] || { python3 "$HERE/host/scvolt.py" --limits; echo; die "give at least one rail"; }

# --- 1. validate the request first: no hardware is touched if it is bad
FRAME_OUT="$(python3 "$HERE/host/scvolt.py" "$@")" || { echo "$FRAME_OUT" >&2; exit 2; }
case "$*" in *--limits*|*--selfcheck*) echo "$FRAME_OUT"; exit 0 ;; esac
grep -q '^frame: ' <<<"$FRAME_OUT" || { echo "$FRAME_OUT" >&2; die "no frame built"; }

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
[ -f "$BIT" ] || die "bitstream not found: $BIT (build it: cd rtl && vivado -mode batch -source build.tcl)"
[ -x "$VIVADO" ] || die "vivado not found at $VIVADO"
echo "loading $(basename "$BIT") ..."
# Vivado must source a FILE. `-source /dev/stdin` with a heredoc silently does
# nothing: vivado reads stdin itself, runs no script, and exits in three
# seconds -- which looked exactly like a successful load and cost a bring-up.
tcl="$(mktemp -t scvolt-load-XXXXXX.tcl)"
log="$(mktemp -t scvolt-load-XXXXXX.log)"
trap 'rm -f "$tcl" "$log"' EXIT
cat > "$tcl" <<TCL
open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target [lindex [get_hw_targets] 0]
set d [lindex [get_hw_devices] 0]
current_hw_device \$d
set_property PROGRAM.FILE {$BIT} \$d
program_hw_devices \$d
puts "PROGRAM_OK"
close_hw_target
TCL
"$VIVADO" -mode batch -nojournal -nolog -source "$tcl" > "$log" 2>&1 || true
if ! grep -q PROGRAM_OK "$log"; then
    echo "--- vivado ---" >&2; tail -20 "$log" >&2
    die "programming failed (no PROGRAM_OK)"
fi
grep -qi "End of startup status: HIGH" "$log" \
    && echo "loaded (End of startup status: HIGH)." \
    || echo "loaded (vivado reported no startup status -- verify before trusting it)."

# --- 5. speak to the satellite controller
exec python3 "$HERE/host/scset.py" "$@"

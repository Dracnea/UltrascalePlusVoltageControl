#!/usr/bin/env bash
# changeVoltage — set a Varium C1100 / U55N or Alveo U55C's rails without TeamRedMiner.
# Also sets VCCINT on a TUL TH53 / TH55, and the rails of an SQRL FK33.
#
#   ./changeVoltage.sh --vccint 700                       core only
#   ./changeVoltage.sh --vccint 700 --vccbram min --vccmem min
#   ./changeVoltage.sh --limits                           what each rail allows
#   ./changeVoltage.sh --enable-menu                      start the SC's own menu
#   ./changeVoltage.sh --survey --out survey.json         TH53/TH55: what else is on the bus
#   SCLINK_FTDI=<serial> ./changeVoltage.sh --vccint min --vccbram min --vccmem min
#                                                         FK33, no Vivado: the FTDI is driven directly
#
# Each rail takes a millivolt number, `min` (the SC firmware's floor) or
# `default` (the card's stock setpoint). Setting the memory rails to `min` is
# the point of taking all three: a design with no BRAM and no HBM has no reason
# to hold VCCBRAM at 850 mV or VCCMEM at 1200 mV, and a compute-bound design
# that instantiates 0 BRAM tiles, 0 URAM and no HBM is exactly that shape.
#
# The card is identified from the part Vivado reports, before anything is
# loaded, and that picks both the rules the request is checked against and the
# bridge (rtl/scbridge_c1100.bit, rtl/scbridge_u55c.bit, rtl/vrmbridge_th53.bit
# or rtl/vrmbridge_th55.bit; SCVOLT_BIT overrides). On a host with more than one
# card, SCLINK_SERIAL picks the JTAG target; SCLINK_XVC=host:port reaches a card
# through a Xilinx Virtual Cable server instead of a local cable.
#
# It loads a small bitstream of ours (scbridge, ~300 LUT) purely to get a UART
# onto the satellite controller's pins, then speaks the protocol in scvolt.py.
# TeamRedMiner is not involved and no .bxz is needed.
#
# TH53 / TH55: there is no satellite controller in the way. The VCCINT
# regulator is a PMBus device on fabric pins, so the bridge (vrmbridge) is an
# I2C master and host/vrmset.py speaks PMBus to it. VCCINT only.
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

limits() {
    python3 "$HERE/host/scvolt.py" --limits; echo; python3 "$HERE/host/vrmset.py" --limits
    echo; python3 "$HERE/host/fk33set.py" --limits
}
[ $# -gt 0 ] || { limits; echo; die "give at least one rail"; }

# --limits and --selfcheck touch no hardware.
case "$*" in
    *--limits*)
        limits
        exit 0 ;;
    *--selfcheck*)
        python3 "$HERE/host/scvolt.py" --selfcheck && python3 "$HERE/host/vrmset.py" --selftest \
            && python3 "$HERE/host/fk33set.py" --selftest && python3 "$HERE/host/boardguard.py" --selftest
        exit $? ;;
esac

# --- SCLINK_FTDI=<serial>: the Vivado-free path, for one-SLR boards on a local
#     FTDI (the SQRL FK33). host/ftdijtag.py reads the IDCODE, loads the bridge
#     with the standard JTAG configuration sequence and shifts its register; the
#     same guard and checks as the Vivado path run first.
if [ -n "${SCLINK_FTDI:-}" ]; then
    for xzf in "$HERE"/rtl/prebuilt/vrmbridge_fk33.bit.xz; do
        raw="$HERE/rtl/vrmbridge_fk33.bit"
        [ -f "$xzf" ] && [ ! -f "$raw" ] && { echo "unpacking $(basename "$xzf")"; xz -dc "$xzf" > "$raw"; }
    done
    echo "identifying the card ..."
    ID="$(python3 "$HERE/host/ftdijtag.py" --serial "$SCLINK_FTDI" --idcode)" || die "could not read the IDCODE over FTDI $SCLINK_FTDI"
    PART_ID="$(sed -n 's/.*PART \([^ ]*\).*/\1/p' <<<"$ID")"
    echo "  $ID"
    GUARD="$(python3 "$HERE/host/boardguard.py" --part "$PART_ID" --target "ftdi:$SCLINK_FTDI" "$@")" || exit 4
    echo "$GUARD"
    case "$GUARD" in *"an FK33"*) ;; *) die "SCLINK_FTDI drives the SQRL FK33 only; use the Vivado path for this card" ;; esac
    python3 "$HERE/host/fk33set.py" --check "$@" || exit 2
    BIT="${SCVOLT_BIT:-$HERE/rtl/vrmbridge_fk33.bit}"
    [ -f "$BIT" ] || die "bitstream not found: $BIT (build it: cd rtl && BOARD=fk33 vivado -mode batch -source build.tcl)"
    python3 "$HERE/host/ftdijtag.py" --serial "$SCLINK_FTDI" --load "$BIT" || die "loading the bridge failed"
    exec python3 "$HERE/host/fk33set.py" "$@"
fi

[ -x "$VIVADO" ] || die "vivado not found at $VIVADO"
SERIAL="${SCLINK_SERIAL:-}"
XVC="${SCLINK_XVC:-}"
tcl="$(mktemp -t scvolt-load-XXXXXX.tcl)"
log="$(mktemp -t scvolt-load-XXXXXX.log)"
trap 'rm -f "$tcl" "$log"' EXIT

# Tcl that opens the one target this run is about. Shared by both passes.
open_target_tcl() {
    cat <<TCL
open_hw_manager
connect_hw_server -allow_non_jtag
if {"$XVC" ne ""} {
    open_hw_target -xvc_url TCP:$XVC
    set t [current_hw_target]
} else {
    if {"$SERIAL" ne ""} {
        set t [lsearch -inline -glob [get_hw_targets] *$SERIAL*]
        if {\$t eq ""} { error "no hw_target matching $SERIAL" }
    } else {
        if {[llength [get_hw_targets]] != 1} { error "more than one hw_target -- set SCLINK_SERIAL" }
        set t [lindex [get_hw_targets] 0]
    }
    open_hw_target \$t
}
puts "TARGET \$t"
set d [lindex [get_hw_devices] 0]
current_hw_device \$d
set part [get_property PART \$d]
puts "PART \$part"
TCL
}

# --- 0. a card reached over XVC: make sure the far end is not an Osprey E3xx
#        box before Vivado so much as opens it (host/boardguard.py)
if [ -n "$XVC" ]; then
    python3 "$HERE/host/boardguard.py" --xvc-only --xvc "$XVC" || exit 4
fi

# --- 1. identify the card, read-only, and validate the request for it: no
#        bitstream is loaded if the request is bad
{ open_target_tcl; echo "close_hw_target"; } > "$tcl"
echo "identifying the card ..."
"$VIVADO" -mode batch -nojournal -nolog -source "$tcl" > "$log" 2>&1 || true
PART_ID="$(sed -n 's/^PART //p' "$log" | head -1)"
TARGET_ID="$(sed -n 's/^TARGET //p' "$log" | head -1)"
if [ -z "$PART_ID" ]; then
    echo "--- vivado ---" >&2; tail -20 "$log" >&2
    die "could not read the device's part over JTAG"
fi
case "$PART_ID" in
    *u55n*|*u55c*) FAMILY=sc ;;
    *vu33p*|*vu35p*) FAMILY=vrm ;;
    *) die "unsupported part $PART_ID" ;;
esac
echo "  PART $PART_ID"

# --enable-menu builds no rail frame -- it sends 0x09, which starts the
# controller's own peripheral-test menu on its FTDI UART -- so it skips the
# frame check but still takes the bridge path below, because 0x09 travels over
# the same link as everything else.
if [ "$FAMILY" = vrm ]; then
    case "$*" in *--enable-menu*) die "--enable-menu is a satellite-controller command; a TH53/TH55 has none" ;; esac
    # The Osprey E3xx uses the same dies; refuse it on the die variant before
    # anything is loaded. (The XVC checks already ran in step 0.)
    GUARD="$(python3 "$HERE/host/boardguard.py" --part "$PART_ID" --target "$TARGET_ID" "$@")" || exit 4
    echo "$GUARD"
    case "$GUARD" in
        *"an FK33"*) FAMILY=fk33
                     python3 "$HERE/host/fk33set.py" --check "$@" || exit 2 ;;
        *)           python3 "$HERE/host/vrmset.py" --check "$@" || exit 2 ;;
    esac
else
    case "$*" in *--survey*) die "--survey is for the TH53/TH55's PMBus; this card has a satellite controller" ;; esac
    case "$*" in
        *--enable-menu*)
            case "$*" in
                *--vccint*|*--vccbram*|*--vccmem*)
                    die "--enable-menu sets no rails; run it on its own" ;;
            esac
            ;;
        *)
            FRAME_OUT="$(python3 "$HERE/host/scvolt.py" "$@")" || { echo "$FRAME_OUT" >&2; exit 2; }
            grep -q '^frame: ' <<<"$FRAME_OUT" || { echo "$FRAME_OUT" >&2; die "no frame built"; }
            ;;
    esac
fi

# --- 2. the card has to be off the PCIe bus: this programs over JTAG, and
#        dropping the link under a bound driver is how you get bus errors
# (TH53/TH55s are not PCIe endpoints, and a C1100 elsewhere in the same host
# must not block them.)
if [ "$FAMILY" = sc ] && [ -n "$PCI" ] && [ -d "/sys/bus/pci/devices/$PCI" ]; then
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
# so the Tcl reads PART off the device again and picks the matching file, and
# refuses a part it does not know rather than guessing.
[ -z "$BIT" ] || [ -f "$BIT" ] || die "bitstream not found: $BIT"
# Prebuilt bridges ship xz-compressed in rtl/prebuilt/ (~50 KB against 28-85 MB
# raw); unpack any that has not been built or unpacked locally yet.
for xzf in "$HERE"/rtl/prebuilt/*bridge_*.bit.xz; do
    [ -f "$xzf" ] || continue
    raw="$HERE/rtl/$(basename "${xzf%.xz}")"
    [ -f "$raw" ] || { echo "unpacking $(basename "$xzf")"; xz -dc "$xzf" > "$raw"; }
done
# Vivado must source a FILE. `-source /dev/stdin` with a heredoc silently does
# nothing: vivado reads stdin itself, runs no script, and exits in three
# seconds -- which looked exactly like a successful load and cost a bring-up.
{ open_target_tcl; cat <<TCL
set bit {$BIT}
if {\$bit eq ""} {
    switch -glob -- [string tolower \$part] {
        *u55c*  { set bit {$HERE/rtl/scbridge_u55c.bit} }
        *u55n*  { set bit {$HERE/rtl/scbridge_c1100.bit} }
        *vu33p_civ* { set bit {$HERE/rtl/vrmbridge_th53.bit} }
        *vu33p* { if {"$FAMILY" ne "fk33"} { error "a plain xcvu33p that the guard did not accept as an FK33" }
                  set bit {$HERE/rtl/vrmbridge_fk33.bit} }
        *vu35p* { set bit {$HERE/rtl/vrmbridge_th55.bit} }
        default { error "unsupported part \$part -- set SCVOLT_BIT to a bridge built for it" }
    }
}
if {"\$part" ne "$PART_ID"} { error "the device changed between identify and load: \$part" }
if {![file exists \$bit]} { error "bitstream not found: \$bit (build it: cd rtl && BOARD=... vivado -mode batch -source build.tcl)" }
puts "BIT \$bit"
set_property PROGRAM.FILE \$bit \$d
program_hw_devices \$d
puts "PROGRAM_OK"
close_hw_target
TCL
} > "$tcl"
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

# --- 5. speak to the satellite controller, or to the regulator
if [ "$FAMILY" = fk33 ]; then
    exec python3 "$HERE/host/fk33set.py" "$@"
fi
if [ "$FAMILY" = vrm ]; then
    exec python3 "$HERE/host/vrmset.py" "$@"
fi
exec python3 "$HERE/host/scset.py" "$@"

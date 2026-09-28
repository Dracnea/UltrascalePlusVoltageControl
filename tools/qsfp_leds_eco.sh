#!/usr/bin/env bash
# Drive the C1100's QSFP LEDs off in an already-routed image, then prove the
# new bitstream differs from the original only in frame data.
#
#   tools/qsfp_leds_eco.sh <routed.dcp> <original.bit> <out.bit> [PROP=VALUE ...]
#
# Runs qsfp_leds_eco.tcl (timing, routing, DRC and pin gates), then bitregs.py:
# every configuration-register write outside frame data -- USR_ACCESS, COR0/1,
# compression, the IDCODE of each SLR -- must match the original image.
# Output is mirrored to <out>.eco.log, which is deleted when the run succeeds.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ $# -ge 3 ] || { sed -n '2,10p' "$0"; exit 2; }
LOG="${3%.bit}.eco.log"
exec > >(tee "$LOG") 2>&1
VIVADO=${VIVADO:-vivado}
"$VIVADO" -mode batch -nojournal -nolog -source "$HERE/qsfp_leds_eco.tcl" -tclargs "$1" "$3" "${@:4}"
grep -q '### ECO done' "$LOG" || { echo "qsfp_leds_eco: no ECO done marker"; exit 1; }
python3 "$HERE/bitregs.py" "$2" "$3"
md5sum "$3"
echo "qsfp_leds_eco: OK"
rm -f -- "$LOG"

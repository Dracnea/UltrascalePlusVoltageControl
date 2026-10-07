#!/usr/bin/env bash
# Simulate pmbus_i2c against a behavioural PMBus regulator (Vivado xsim).
set -euo pipefail
cd "$(dirname "$0")"
LOG="${LOG:-sim.log}"
BIN="${VIVADO_BIN:-$HOME/Xilinx/2026.1/Vivado/bin}"
# Twice: against a regulator that stretches SCL after every ACK, and one that
# does not.
: > "$LOG"
for stretch in 30_000 0; do
    {
        echo "#### slave clock stretch: ${stretch} ns"
        "$BIN/xvlog" -sv -d STRETCH=$stretch ../rtl/pmbus_i2c.sv tb_pmbus_i2c.sv
        "$BIN/xelab" -debug off tb -s tb_sim
        "$BIN/xsim" tb_sim -R
    } 2>&1 | tee -a "$LOG"
done
[ "$(grep -c '== 0 failure(s)' "$LOG")" -eq 2 ]

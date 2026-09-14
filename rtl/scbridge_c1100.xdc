# Varium C1100 / Alveo U55N (xcu55n) pin constraints for scbridge.
#
# Pin data traces to Corundum's public Alveo targets
# (github.com/corundum/corundum, fpga/mqnic/Alveo), which is where the
# satellite-controller UART and GPIO assignments for this card family are
# published. Verified through place-and-route on this card.

# 100 MHz user clock
set_property PACKAGE_PIN BK43 [get_ports clk_p]
set_property PACKAGE_PIN BK44 [get_ports clk_n]
set_property IOSTANDARD LVDS  [get_ports {clk_p clk_n}]
create_clock -name clk100 -period 10.000 [get_ports clk_p]

# hbm_cattrip MUST be driven low. Left floating, the satellite controller reads
# an HBM catastrophic-temperature trip and powers the card off -- which looks
# exactly like a dead board.
set_property PACKAGE_PIN BE45     [get_ports hbm_cattrip]
set_property IOSTANDARD LVCMOS18  [get_ports hbm_cattrip]

# ---- satellite-controller UART -----------------------------------------------
# Both are tri-stated except while a byte is going out. PULLDOWN is what makes
# the direction test meaningful: a line that still reads high with a pulldown on
# it is being driven by the controller.
set_property PACKAGE_PIN BH42    [get_ports sc_txd]
set_property PACKAGE_PIN BJ42    [get_ports sc_rxd]
set_property IOSTANDARD LVCMOS18 [get_ports {sc_txd sc_rxd}]
set_property PULLTYPE PULLDOWN   [get_ports {sc_txd sc_rxd}]
set_property DRIVE 4             [get_ports {sc_txd sc_rxd}]
set_property SLEW SLOW           [get_ports {sc_txd sc_rxd}]

set_property PACKAGE_PIN BE46    [get_ports {sc_gpio[0]}]
set_property PACKAGE_PIN BH46    [get_ports {sc_gpio[1]}]
set_property PACKAGE_PIN BF45    [get_ports {sc_gpio[2]}]
set_property PACKAGE_PIN BF46    [get_ports {sc_gpio[3]}]
set_property IOSTANDARD LVCMOS18 [get_ports {sc_gpio[*]}]

# The UART and the JTAG shift register are asynchronous to each other by design.
set_false_path -from [get_ports sc_gpio[*]]

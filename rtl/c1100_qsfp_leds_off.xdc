# Varium C1100 / Alveo U55N: drive the six QSFP28 LEDs OFF.
#
# Reusable. Any C1100 top can add these three 2-bit output ports, tie them to
# 2'b00, and read this file:
#
#     output wire [1:0] qsfp_led_act,
#     output wire [1:0] qsfp_led_stat_g,
#     output wire [1:0] qsfp_led_stat_y,
#     ...
#     assign qsfp_led_act    = 2'b00;
#     assign qsfp_led_stat_g = 2'b00;
#     assign qsfp_led_stat_y = 2'b00;
#
# The LEDs are active-high (confirmed on a card: driving these pins low turns
# all six off). A design that leaves the pins unused gets UNUSEDPIN PULLUP,
# which is what lights them. Driving them explicitly, rather than switching
# UNUSEDPIN to PULLDOWN, leaves every other unused pin exactly as it was.
#
# Pin data: AMD's Varium C1100 board files (component qsfp28_leds, pins
# QSFP28_{0,1}_ACTIVITY_LED / _LINK_STAT_LEDG / _LINK_STAT_LEDY) and Corundum's
# public AU55N target (fpga/mqnic/Alveo/fpga_25g/fpga_au55.xdc). All six are in
# bank 68 at LVCMOS18. Output buffers only: no LUTs, no timing impact.
set_property -dict {PACKAGE_PIN BL13 IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8} [get_ports {qsfp_led_act[0]}]
set_property -dict {PACKAGE_PIN BK11 IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8} [get_ports {qsfp_led_stat_g[0]}]
set_property -dict {PACKAGE_PIN BJ11 IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8} [get_ports {qsfp_led_stat_y[0]}]
set_property -dict {PACKAGE_PIN BK14 IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8} [get_ports {qsfp_led_act[1]}]
set_property -dict {PACKAGE_PIN BK15 IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8} [get_ports {qsfp_led_stat_g[1]}]
set_property -dict {PACKAGE_PIN BL12 IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8} [get_ports {qsfp_led_stat_y[1]}]
set_false_path -to [get_ports {qsfp_led_act[*] qsfp_led_stat_g[*] qsfp_led_stat_y[*]}]

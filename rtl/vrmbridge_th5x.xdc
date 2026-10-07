# TUL TH53 (xcvu33p) / TH55 (xcvu35p) pin constraints for vrmbridge.
#
# The two boards are sisters and share every pin the bridge touches. All of it
# was measured on the boards (clock census, then a pull-up sweep of bank 67 and
# an I2C scan of each pulled-up pair).

# 100 MHz LVDS reference. F13/F12 is DEAD on these boards -- the oscillator is
# on BB18/BC18 and needs both termination properties to be seen.
set_property PACKAGE_PIN BB18 [get_ports clk_p]
set_property PACKAGE_PIN BC18 [get_ports clk_n]
set_property IOSTANDARD LVDS  [get_ports {clk_p clk_n}]
set_property DIFF_TERM_ADV TERM_100 [get_ports clk_p]
set_property DQS_BIAS TRUE [get_ports clk_p]
create_clock -name clk100 -period 10.000 [get_ports clk_p]

# VCCINT regulator PMBus: Infineon, 7-bit address 0x60, bank 67, external
# pull-ups present. The pins are only ever driven low or released.
set_property PACKAGE_PIN L12     [get_ports vrm_scl]
set_property PACKAGE_PIN L13     [get_ports vrm_sda]
set_property IOSTANDARD LVCMOS18 [get_ports {vrm_scl vrm_sda}]
set_property DRIVE 4             [get_ports {vrm_scl vrm_sda}]
set_property SLEW SLOW           [get_ports {vrm_scl vrm_sda}]

# The bus is slow and asynchronous to everything; nothing here is a timed path.
set_false_path -from [get_ports {vrm_scl vrm_sda}]
set_false_path -to   [get_ports {vrm_scl vrm_sda}]

set_property CFGBVS GND [current_design]
set_property CONFIG_VOLTAGE 1.8 [current_design]

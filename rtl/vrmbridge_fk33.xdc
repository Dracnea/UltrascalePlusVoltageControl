# SQRL FK33 (plain xcvu33p) pin constraints for vrmbridge.
#
# From SQRL's own board files (board_files/sqrl_fk33/1.1): the I2C bus "to
# PMIC" that their example design and their bridge use to set the rails, and
# the board's 200 MHz LVDS oscillator. Nothing else is driven.

# 200 MHz LVDS reference. Unlike the TH53/TH55 it needs no
# termination properties to be seen.
set_property PACKAGE_PIN BC26 [get_ports clk_p]
set_property PACKAGE_PIN BC27 [get_ports clk_n]
set_property IOSTANDARD LVDS  [get_ports {clk_p clk_n}]
create_clock -name clk200 -period 5.000 [get_ports clk_p]

# Rail-control I2C, LVCMOS18 per the board files. The pins are only
# ever driven low or released.
set_property PACKAGE_PIN BB24    [get_ports vrm_scl]
set_property PACKAGE_PIN BA24    [get_ports vrm_sda]
set_property IOSTANDARD LVCMOS18 [get_ports {vrm_scl vrm_sda}]
set_property DRIVE 4             [get_ports {vrm_scl vrm_sda}]
set_property SLEW SLOW           [get_ports {vrm_scl vrm_sda}]

set_false_path -from [get_ports {vrm_scl vrm_sda}]
set_false_path -to   [get_ports {vrm_scl vrm_sda}]

set_property CFGBVS GND [current_design]
set_property CONFIG_VOLTAGE 1.8 [current_design]

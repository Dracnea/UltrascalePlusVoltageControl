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

# SYSMON auxiliary analog inputs (bank 66): the board's sensors, as SQRL's example
# design enables them (OhGodAPet/SQRL_FK33 projects/fk33_example.tcl, Vaux 0/4/5/8/12/13;
# readings named in scripts/fk33_jtagaxi.tcl). Pins from the xcvu33p-fsvh2104 package
# file (IO_*_ADnP/N_66). Inputs only, used as analog: the IOSTANDARD is for DRC and
# drives nothing. Compiled in with VRM_VAUX (build.tcl sets it for the FK33).
set_property PACKAGE_PIN AV33 [get_ports vaux0_p]
set_property PACKAGE_PIN AW33 [get_ports vaux0_n]
set_property PACKAGE_PIN BB34 [get_ports vaux4_p]
set_property PACKAGE_PIN BB35 [get_ports vaux4_n]
set_property PACKAGE_PIN BC35 [get_ports vaux5_p]
set_property PACKAGE_PIN BD35 [get_ports vaux5_n]
set_property PACKAGE_PIN AY34 [get_ports vaux8_p]
set_property PACKAGE_PIN BA34 [get_ports vaux8_n]
set_property PACKAGE_PIN BB36 [get_ports vaux12_p]
set_property PACKAGE_PIN BC36 [get_ports vaux12_n]
set_property PACKAGE_PIN BE34 [get_ports vaux13_p]
set_property PACKAGE_PIN BE35 [get_ports vaux13_n]
set_property IOSTANDARD LVCMOS18 [get_ports {vaux*_p vaux*_n}]

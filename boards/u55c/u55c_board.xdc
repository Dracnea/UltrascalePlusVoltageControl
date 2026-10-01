# Alveo U55C (xcu55c-fsvh2892-2L-e) pins for u55c_board_top. From the board's
# part0_pins.xml (FPGA-Archive/Boards/U55C): SYSCLK3 and HBM_CATTRIP.
set_property PACKAGE_PIN BK43 [get_ports clk_p]
set_property PACKAGE_PIN BK44 [get_ports clk_n]
set_property IOSTANDARD LVDS  [get_ports {clk_p clk_n}]
create_clock -name sysclk3 -period 10.000 [get_ports clk_p]
create_clock -name tck -period 50.000 [get_pins u_bscan/TCK]
set_clock_groups -asynchronous -group [get_clocks sysclk3] -group [get_clocks tck]

set_property PACKAGE_PIN BE45    [get_ports hbm_cattrip]
set_property IOSTANDARD LVCMOS18 [get_ports hbm_cattrip]
set_false_path -to [get_ports hbm_cattrip]

set_property CFGBVS GND [current_design]
set_property CONFIG_VOLTAGE 1.8 [current_design]
set_property BITSTREAM.CONFIG.UNUSEDPIN PULLUP [current_design]
set_property BITSTREAM.CONFIG.OVERTEMPSHUTDOWN Enable [current_design]

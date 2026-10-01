# Alveo U55C parking / pin-check image.
# vivado -mode batch -source build.tcl   ->  u55c-board-ledsoff.bit
set here [file dirname [file normalize [info script]]]
set part xcu55c-fsvh2892-2L-e
create_project -in_memory -part $part
read_verilog -sv [file join $here u55c_board_top.sv]
read_xdc [file join $here u55c_board.xdc]
read_xdc [file join $here .. .. rtl c1100_qsfp_leds_off.xdc]   ;# the same six pins on the U55C
synth_design -top u55c_board_top -part $part
opt_design; place_design; route_design
report_utilization -file [file join $here util.rpt]
set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
puts "### WNS $wns"
if {$wns ne "" && $wns < 0} { error "timing not met: $wns" }
# The LEDs and cattrip must land on their pins, or the image is wrong for this board.
foreach {port pin} {qsfp_led_act[0] BL13 qsfp_led_stat_g[0] BK11 qsfp_led_stat_y[0] BJ11
                    qsfp_led_act[1] BK14 qsfp_led_stat_g[1] BK15 qsfp_led_stat_y[1] BL12
                    hbm_cattrip BE45 clk_p BK43} {
    set got [get_property PACKAGE_PIN [get_ports $port]]
    if {$got ne $pin} { error "pin gate: $port on '$got', want $pin" }
}
puts "### PIN GATE PASS"
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
set_property BITSTREAM.CONFIG.USR_ACCESS 0x55C0B0A1 [current_design]
write_bitstream -force [file join $here u55c-board-ledsoff.bit]
puts "### BUILD OK"

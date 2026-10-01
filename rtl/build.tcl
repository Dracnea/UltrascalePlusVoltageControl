# Build scbridge for the Varium C1100 / Alveo U55N, or the Alveo U55C.
#
#   vivado -mode batch -source build.tcl                  C1100 -> scbridge_c1100.bit
#   BOARD=u55c vivado -mode batch -source build.tcl       U55C  -> scbridge_u55c.bit
#   PART=... vivado -mode batch -source build.tcl         override the part
#
# The two cards share the FSVH2892 package and, by AMD's board files, the same
# pins for everything the bridge touches (SC UART, clock, hbm_cattrip, QSFP
# LEDs), so one XDC serves both; only the part and the output name change.
set board [expr {[info exists ::env(BOARD)] ? [string tolower $::env(BOARD)] : "c1100"}]
switch -- $board {
    c1100   { set defpart xcu55n-fsvh2892-2LV-e }
    u55c    { set defpart xcu55c-fsvh2892-2L-e }
    default { error "BOARD must be c1100 or u55c, not '$board'" }
}
set part [expr {[info exists ::env(PART)] ? $::env(PART) : $defpart}]
set here [file dirname [file normalize [info script]]]

create_project -force scbridge [file join $here build] -part $part
add_files [list [file join $here sc_uart.sv] [file join $here scbridge_top.sv]]
set_property file_type SystemVerilog [get_files *.sv]
add_files -fileset constrs_1 [list [file join $here scbridge_c1100.xdc] \
                                  [file join $here c1100_qsfp_leds_off.xdc]]
set_property top scbridge_top [current_fileset]
update_compile_order -fileset sources_1

synth_design -top scbridge_top -part $part
opt_design
place_design
route_design
report_utilization -file [file join $here util.rpt]
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
puts "### WNS: $wns"
if {$wns ne "" && $wns < 0} { error "timing not met: WNS $wns" }

# The six QSFP LEDs must land on their pins, or they stay lit.
foreach {port pin} {qsfp_led_act[0] BL13 qsfp_led_stat_g[0] BK11 qsfp_led_stat_y[0] BJ11
                    qsfp_led_act[1] BK14 qsfp_led_stat_g[1] BK15 qsfp_led_stat_y[1] BL12} {
    set got [get_property PACKAGE_PIN [get_ports $port]]
    if {$got ne $pin} { error "LED gate: $port on '$got', want $pin" }
}
puts "### LED gate PASS: six QSFP LEDs driven off"

# Uncompressed, so a probe image is obvious next to a compressed application one.
set_property BITSTREAM.GENERAL.COMPRESS FALSE [current_design]
write_bitstream -force [file join $here scbridge_$board.bit]
puts "### BUILD OK: [file join $here scbridge_$board.bit]"

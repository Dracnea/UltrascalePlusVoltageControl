# Build a bridge bitstream.
#
#   vivado -mode batch -source build.tcl                  C1100 -> scbridge_c1100.bit
#   BOARD=u55c vivado -mode batch -source build.tcl       U55C  -> scbridge_u55c.bit
#   BOARD=th53 vivado -mode batch -source build.tcl       TH53  -> vrmbridge_th53.bit
#   BOARD=th55 vivado -mode batch -source build.tcl       TH55  -> vrmbridge_th55.bit
#   BOARD=fk33 vivado -mode batch -source build.tcl       FK33  -> vrmbridge_fk33.bit
#   PART=... vivado -mode batch -source build.tcl         override the part
#
# The C1100 and U55C share the FSVH2892 package and, by AMD's board files, the
# same pins for everything scbridge touches (SC UART, clock, hbm_cattrip, QSFP
# LEDs), so one XDC serves both. The TH53 and TH55 have no satellite controller
# in the rail path -- the regulator is on a fabric I2C bus -- so they get
# vrmbridge instead, again with one XDC for the pair. The SQRL FK33 has its
# rail controls on a fabric I2C bus too, on other pins and a 200 MHz clock.
set board [expr {[info exists ::env(BOARD)] ? [string tolower $::env(BOARD)] : "c1100"}]
switch -- $board {
    c1100   { set defpart xcu55n-fsvh2892-2LV-e;      set kind sc }
    u55c    { set defpart xcu55c-fsvh2892-2L-e;       set kind sc }
    th53    { set defpart xcvu33p_CIV-fsvh2104-2L-e;  set kind vrm }
    th55    { set defpart xcvu35p-fsvh2104-2LV-e;     set kind vrm }
    fk33    { set defpart xcvu33p-fsvh2104-2-e;       set kind vrm }
    default { error "BOARD must be c1100, u55c, th53, th55 or fk33, not '$board'" }
}
set part [expr {[info exists ::env(PART)] ? $::env(PART) : $defpart}]
set here [file dirname [file normalize [info script]]]

if {$kind eq "vrm"} {
    create_project -force vrmbridge_$board [file join $here build_$board] -part $part
    add_files [list [file join $here pmbus_i2c.sv] [file join $here vrmbridge_top.sv]]
    set_property file_type SystemVerilog [get_files *.sv]
    if {$board eq "fk33"} {
        set xdc vrmbridge_fk33.xdc; set clk_hz 200000000
        set pins {vrm_scl BB24 vrm_sda BA24 clk_p BC26}
    } else {
        set xdc vrmbridge_th5x.xdc; set clk_hz 100000000
        set pins {vrm_scl L12 vrm_sda L13 clk_p BB18}
    }
    add_files -fileset constrs_1 [list [file join $here $xdc]]
    set_property top vrmbridge_top [current_fileset]
    update_compile_order -fileset sources_1

    synth_design -top vrmbridge_top -part $part -generic CLK_HZ=$clk_hz
    opt_design
    place_design
    route_design
    report_utilization -file [file join $here util_$board.rpt]
    set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
    puts "### WNS: $wns"
    if {$wns ne "" && $wns < 0} { error "timing not met: WNS $wns" }
    foreach {port pin} $pins {
        set got [get_property PACKAGE_PIN [get_ports $port]]
        if {$got ne $pin} { error "pin gate: $port on '$got', want $pin" }
    }
    puts "### pin gate PASS: $pins"
    set_property BITSTREAM.GENERAL.COMPRESS FALSE [current_design]
    write_bitstream -force [file join $here vrmbridge_$board.bit]
    puts "### BUILD OK: [file join $here vrmbridge_$board.bit]"
    return
}

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

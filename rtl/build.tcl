# Build scbridge for the Varium C1100 / Alveo U55N.
#
#   vivado -mode batch -source build.tcl
#   PART=xcu55n-fsvh2892-2LV-e vivado -mode batch -source build.tcl
#
# Produces scbridge_c1100.bit next to this script. Nothing here is card- or
# site-specific beyond the part and the XDC.
set part [expr {[info exists ::env(PART)] ? $::env(PART) : "xcu55n-fsvh2892-2LV-e"}]
set here [file dirname [file normalize [info script]]]

create_project -force scbridge [file join $here build] -part $part
add_files [list [file join $here sc_uart.sv] [file join $here scbridge_top.sv]]
set_property file_type SystemVerilog [get_files *.sv]
add_files -fileset constrs_1 [file join $here scbridge_c1100.xdc]
set_property top scbridge_top [current_fileset]
update_compile_order -fileset sources_1

synth_design -top scbridge_top -part $part
opt_design
place_design
route_design
report_utilization -file [file join $here util.rpt]
puts "### WNS: [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]"

# Uncompressed, so a probe image is obvious next to a compressed application one.
set_property BITSTREAM.GENERAL.COMPRESS FALSE [current_design]
write_bitstream -force [file join $here scbridge_c1100.bit]
puts "### BUILD OK: [file join $here scbridge_c1100.bit]"

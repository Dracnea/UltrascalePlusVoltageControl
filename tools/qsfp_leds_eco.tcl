# qsfp_leds_eco.tcl -- drive the Varium C1100's six QSFP LEDs off in an image that
# is already routed, without re-implementing it.
#
#   vivado -mode batch -source qsfp_leds_eco.tcl -tclargs <routed.dcp> <out.bit> [PROP=VALUE ...]
#
# PROP=VALUE pairs are set on the design just before write_bitstream. They are
# for flows that save the routed checkpoint BEFORE applying bitstream
# properties: if yours sets BITSTREAM.GENERAL.COMPRESS TRUE after its last
# write_checkpoint, pass BITSTREAM.GENERAL.COMPRESS=TRUE here or the new image
# comes out uncompressed. qsfp_leds_eco.sh's register diff against the
# original image is what catches a missed one.
#
# An application top that leaves the six LED pins unused gets UNUSEDPIN
# PULLUP, which lights them (the LEDs are active-high, confirmed on a card).
# This adds six output ports on the pins from rtl/c1100_qsfp_leds_off.xdc, each
# an OBUF fed from a GND cell, places the OBUFs on their IOBs, routes only the
# new nets and writes the bitstream with the checkpoint's own BITSTREAM.*
# properties. The routed design is not touched: every existing cell keeps its
# placement and every existing net its route.
#
# Gates, any of which stops the run before a bitstream is written:
#   - none of the six pins is already used by the design
#   - setup WNS and hold WHS are unchanged from the checkpoint (to the ps)
#   - route status reports no unrouted and no conflicting nets
#   - DRC reports no errors
if {[llength $argv] < 2} { error "usage: -tclargs <routed.dcp> <out.bit> [PROP=VALUE ...]" }
set dcp     [lindex $argv 0]
set out_bit [lindex $argv 1]
set props   [lrange $argv 2 end]
set out_dcp [file rootname $out_bit]_routed.dcp

open_checkpoint $dcp

proc wns {} { get_property SLACK [lindex [get_timing_paths -delay_type max -max_paths 1] 0] }
proc whs {} { get_property SLACK [lindex [get_timing_paths -delay_type min -max_paths 1] 0] }
set wns0 [wns]; set whs0 [whs]
puts "### ECO before: WNS $wns0  WHS $whs0"
puts "### ECO before: USERID [get_property BITSTREAM.CONFIG.USERID [current_design]]\
      USR_ACCESS [get_property BITSTREAM.CONFIG.USR_ACCESS [current_design]]\
      COMPRESS [get_property BITSTREAM.GENERAL.COMPRESS [current_design]]\
      UNUSEDPIN [get_property BITSTREAM.CONFIG.UNUSEDPIN [current_design]]"

# port -> package pin, from rtl/c1100_qsfp_leds_off.xdc
set leds {
    qsfp_led_act[0]    BL13   qsfp_led_stat_g[0] BK11   qsfp_led_stat_y[0] BJ11
    qsfp_led_act[1]    BK14   qsfp_led_stat_g[1] BK15   qsfp_led_stat_y[1] BL12
}
foreach {port pin} $leds {
    set used [get_ports -quiet -of_objects [get_package_pins $pin]]
    if {[llength $used] > 0} { error "pin $pin already carries port $used -- not an unused LED pin here" }
    if {[llength [get_ports -quiet $port]] > 0} { error "port $port already exists" }
}

set gnd [create_cell -reference GND led_eco_gnd]
create_net led_eco_zero
connect_net -net led_eco_zero -objects [get_pins led_eco_gnd/G]

set i 0
set new_nets [list [get_nets led_eco_zero]]
foreach {port pin} $leds {
    create_port -direction OUT $port
    set_property -dict [list PACKAGE_PIN $pin IOSTANDARD LVCMOS18 SLEW SLOW DRIVE 8] [get_ports $port]
    set obuf [create_cell -reference OBUF led_eco_obuf_$i]
    connect_net -net led_eco_zero -objects [get_pins led_eco_obuf_$i/I]
    create_net led_eco_pad_$i
    connect_net -net led_eco_pad_$i -objects [list [get_pins led_eco_obuf_$i/O] [get_ports $port]]
    place_cell led_eco_obuf_$i [get_sites -of_objects [get_package_pins $pin]]
    lappend new_nets [get_nets led_eco_pad_$i]
    incr i
}
set_false_path -to [get_ports {qsfp_led_act[*] qsfp_led_stat_g[*] qsfp_led_stat_y[*]}]

route_design -nets $new_nets

set wns1 [wns]; set whs1 [whs]
puts "### ECO after:  WNS $wns1  WHS $whs1"
if {abs($wns1 - $wns0) > 0.001 || abs($whs1 - $whs0) > 0.001} {
    error "timing moved: WNS $wns0 -> $wns1, WHS $whs0 -> $whs1"
}

set rs [report_route_status -return_string]
puts $rs
foreach {what pat} {unrouted {nets with routing errors:\s*(\d+)} conflict {conflict nets:\s*(\d+)}} {
    if {[regexp -nocase $pat $rs -> n] && $n > 0} { error "route status: $n $what" }
}

report_drc -file [file rootname $out_bit]_drc.rpt
set drc_err [get_drc_violations -quiet -filter {SEVERITY == Error}]
if {[llength $drc_err] > 0} { error "DRC errors: $drc_err" }

foreach {port pin} $leds {
    set got [get_property PACKAGE_PIN [get_ports $port]]
    if {$got ne $pin} { error "led gate: $port on '$got', want $pin" }
}
puts "### ECO gates PASS: timing unchanged, fully routed, DRC clean, six LEDs on their pins"

foreach kv $props {
    if {![regexp {^([^=]+)=(.*)$} $kv -> k v]} { error "bad property '$kv', want PROP=VALUE" }
    set_property $k $v [current_design]
    puts "### ECO property: $k = [get_property $k [current_design]]"
}
write_checkpoint -force $out_dcp
write_bitstream -force $out_bit
puts "### ECO done: $out_bit"

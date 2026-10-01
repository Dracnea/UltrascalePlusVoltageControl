# Load u55c-board-ledsoff.bit on the U55C and measure SYSCLK3 through USER4.
#   vivado -mode batch -source load_and_check.tcl -tclargs <serial>   (card OFF PCIe)
set here [file dirname [file normalize [info script]]]
if {![llength $argv]} { error "usage: vivado -mode batch -source load_and_check.tcl -tclargs <card JTAG serial>" }
set serial [lindex $argv 0]
open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target [lsearch -inline -glob [get_hw_targets] *$serial*]
set d [lindex [get_hw_devices -filter {PART =~ xcu*}] 0]
current_hw_device $d
set bit [file join $here u55c-board-ledsoff.bit]
if {![file exists $bit]} { exec xz -dk [file join $here u55c-board-ledsoff.bit.xz] }
set_property PROGRAM.FILE $bit $d
program_hw_devices $d
refresh_hw_device -update_hw_probes false $d
puts "DONE [get_property REGISTER.CONFIG_STATUS.SLR0.BIT\[14\]_DONE_PIN $d]  USERCODE [get_property REGISTER.USERCODE.SLR0 $d]"
close_hw_target
# Raw JTAG for the BSCAN register: USER4 = 0x23 in the master field, BYPASS
# (0x24) in the other two -- 18 bits on this three-SLR die.
open_hw_target -jtag_mode 1 [lsearch -inline -glob [get_hw_targets] *$serial*]
proc rd {} {
    run_state_hw_jtag IDLE
    scan_ir_hw_jtag 18 -tdi 23924
    return [scan_dr_hw_jtag 64 -tdi 0]
}
proc gray2bin {g} { set b 0; for {set s $g} {$s} {set s [expr {$s >> 1}]} { set b [expr {$b ^ $s}] }; return $b }
set r1 [rd]; set t1 [clock milliseconds]
after 2000
set r2 [rd]; set t2 [clock milliseconds]
foreach r [list $r1 $r2] { puts "RAW $r" }
set m1 [string range $r1 end-7 end]; set c1 [gray2bin [scan [string range $r1 end-15 end-8] %x]]
set c2 [gray2bin [scan [string range $r2 end-15 end-8] %x]]
set dc [expr {($c2 - $c1) & 0xffffffff}]
puts "MAGIC $m1"
puts [format "CLOCK %.3f MHz (%d counts in %d ms)" [expr {$dc / 1000.0 / ($t2 - $t1)}] $dc [expr {$t2 - $t1}]]
close_hw_target

// Testbench: pmbus_i2c against a behavioural PMBus slave with clock stretching.
//   cd sim && ./run.sh
`timescale 1ns/1ps

module pmbus_slave #(parameter [6:0] ADDR = 7'h60, parameter integer STRETCH_NS = 0)
    (inout tri1 scl, inout tri1 sda);
    // Edge-driven: sample on SCL rising, drive on SCL falling, START/STOP on
    // SDA edges while SCL is high. Optionally stretches SCL after every ACK.
    logic sda_drv = 1'b0, scl_drv = 1'b0;
    assign sda = sda_drv ? 1'b0 : 1'bz;
    assign scl = scl_drv ? 1'b0 : 1'bz;

    logic [15:0] regs [0:255];
    initial begin
        foreach (regs[i]) regs[i] = 16'hFFFF;
        regs[8'h21] = 16'd720;    // VOUT_COMMAND
        regs[8'h8B] = 16'd719;    // READ_VOUT
        regs[8'h98] = 16'h0033;   // PMBUS_REVISION
        regs[8'h00] = 16'h0000;   // PAGE
    end
    int sends = 0;

    logic active = 0, txmode = 0, acking = 0, rw = 0, mack = 0, txhi = 0;
    logic [7:0] sh = 0, r = 0, txbyte = 0;
    int bitc = 0, byte_i = 0, tb = 0;

    always @(negedge sda) if (scl === 1'b1) begin          // START / RESTART
        active = 1; txmode = 0; acking = 0; bitc = 0; byte_i = 0;
    end
    always @(posedge sda) if (scl === 1'b1) begin          // STOP
        if (active && !rw && byte_i == 2) sends++;
        active = 0; txmode = 0; acking = 0;
    end

    always @(posedge scl) if (active) begin
        if (txmode) begin
            if (tb == 8) mack = ~sda;
        end else if (!acking && bitc < 8) begin
            sh = {sh[6:0], sda === 1'b0 ? 1'b0 : 1'b1};
            bitc++;
        end
    end

    always @(negedge scl) if (active) begin
        if (!txmode) begin
            if (acking) begin
                acking = 0; bitc = 0; byte_i++;
                // End the ACK and take SCL in the same instant, then set up the
                // next bit while SCL is still held: SDA must never move while
                // SCL is high, or the master sees a STOP/START.
                scl_drv = (STRETCH_NS != 0);
                #50 sda_drv = 0;
                if (rw) begin
                    txmode = 1; txhi = 0; txbyte = regs[r][7:0]; tb = 0;
                    sda_drv = ~txbyte[7];
                end
                if (STRETCH_NS) begin #(STRETCH_NS); scl_drv = 0; end
            end else if (bitc == 8) begin
                case (byte_i)
                    0: if (sh[7:1] == ADDR) begin rw = sh[0]; acking = 1; end
                       else active = 0;
                    1: begin r = sh; acking = 1; end
                    2: begin regs[r][7:0] = sh; acking = 1; end
                    default: begin regs[r][15:8] = sh; acking = 1; end
                endcase
                if (acking) #50 sda_drv = 1;
            end
        end else begin
            if (tb < 7) begin tb++; #50 sda_drv = ~txbyte[7 - tb]; end
            else if (tb == 7) begin tb = 8; #50 sda_drv = 0; end
            else begin
                if (mack && !txhi) begin
                    txhi = 1; txbyte = regs[r][15:8]; tb = 0; #50 sda_drv = ~txbyte[7];
                end else begin
                    txmode = 0; active = 0; sda_drv = 0;
                end
            end
        end
    end
endmodule

module tb;
    logic clk = 0; always #5 clk = ~clk;
    tri1 scl, sda;
    logic go = 0; logic [2:0] mode; logic [6:0] addr; logic [7:0] reg_a; logic [15:0] wdata;
    wire busy, done, addr_ack, reg_ack, data_ack, timeout, scl_low, sda_low;
    wire [15:0] rdata;
    assign scl = scl_low ? 1'b0 : 1'bz;
    assign sda = sda_low ? 1'b0 : 1'bz;

    pmbus_i2c #(.QTR(250), .STRETCH_MAX(400_000)) dut (
        .clk, .go, .mode, .addr, .reg_a, .wdata, .busy, .done, .rdata,
        .addr_ack, .reg_ack, .data_ack, .timeout, .scl_low, .sda_low,
        .scl_in(scl), .sda_in(sda));
    `ifndef STRETCH
`define STRETCH 30_000
`endif
    pmbus_slave #(.ADDR(7'h60), .STRETCH_NS(`STRETCH)) vrm (.scl, .sda);
    logic hold_scl = 0;
    assign scl = hold_scl ? 1'b0 : 1'bz;

    int fails = 0;
    task automatic xfer(input [2:0] m, input [6:0] a, input [7:0] r, input [15:0] d);
        @(posedge clk); mode = m; addr = a; reg_a = r; wdata = d; go = 1;
        @(posedge clk); go = 0;
        @(posedge done);
        @(posedge clk);
        $display("  [t=%0t] m=%0d a=%h r=%h -> aack=%b rack=%b dack=%b to=%b rdata=%h",
                 $time, m, a, r, addr_ack, reg_ack, data_ack, timeout, rdata);
    endtask
    task automatic check(input string what, input logic ok);
        $display("%s %s", ok ? "PASS" : "FAIL", what);
        if (!ok) fails++;
    endtask

    initial begin
        #1000;
        xfer(0, 7'h60, 0, 0);       check("quick probe 0x60 ACKs", addr_ack && !timeout);
        xfer(0, 7'h61, 0, 0);       check("quick probe 0x61 NACKs", !addr_ack);
        xfer(1, 7'h60, 8'h98, 0);   check($sformatf("read byte PMBUS_REVISION = %h", rdata[7:0]),
                                          addr_ack && reg_ack && rdata[7:0] == 8'h33);
        xfer(3, 7'h60, 8'h21, 0);   check($sformatf("read word VOUT_COMMAND = %0d", rdata),
                                          addr_ack && reg_ack && rdata == 16'd720);
        xfer(4, 7'h60, 8'h21, 16'd800); check("write word VOUT_COMMAND 800 acked", addr_ack && reg_ack && data_ack);
        check($sformatf("slave register now %0d", vrm.regs[8'h21]), vrm.regs[8'h21] == 16'd800);
        xfer(3, 7'h60, 8'h21, 0);   check($sformatf("read back VOUT_COMMAND = %0d", rdata), rdata == 16'd800);
        xfer(2, 7'h60, 8'h00, 16'h0001); check("write byte PAGE 1 acked", data_ack && vrm.regs[0][7:0] == 8'h01);
        xfer(5, 7'h60, 8'h03, 0);   check("send byte CLEAR_FAULTS acked", addr_ack && reg_ack && vrm.sends == 1);
        xfer(3, 7'h61, 8'h21, 0);   check("word read from absent 0x61 NACKs, no data", !addr_ack && !reg_ack);
        xfer(3, 7'h60, 8'h8B, 0);   check($sformatf("read word READ_VOUT = %0d", rdata), rdata == 16'd719);
        hold_scl = 1;                // a bus held low must abort, not hang
        xfer(3, 7'h60, 8'h21, 0);   check("stuck SCL -> timeout", timeout && !addr_ack);
        hold_scl = 0;
        #100_000;
        xfer(3, 7'h60, 8'h21, 0);   check($sformatf("recovers after timeout, VOUT_COMMAND = %0d", rdata), rdata == 16'd800);
        $display("== %0d failure(s)", fails);
        $finish;
    end
    initial begin #60_000_000; $display("FAIL: watchdog"); $finish; end
endmodule

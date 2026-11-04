// vrmbridge -- a PMBus master behind a JTAG BSCAN register, plus SYSMONE4.
//
// For boards whose VCCINT regulator sits on an I2C bus the FPGA fabric can
// reach directly: the TUL TH53 (xcvu33p) and TH55 (xcvu35p), where an Infineon
// PMBus regulator answers at 0x60 on L12 (SCL) / L13 (SDA), bank 67. There is
// no satellite controller in the path, so this is a different bridge from
// scbridge -- but it speaks the same BSCAN register, so host/sclink.py drives
// both.
//
// Like scbridge this is a standalone probe: load it, set the rail, load what
// you actually wanted. The regulator keeps its setpoint across
// reconfiguration. `pmbus_i2c` is the reusable half if you would rather set
// the rail from inside a running design.
//
// PROTOCOL
//   One BSCANE2 user register (USER1), 64 bits, shifted LSB first. Every shift
//   writes a command and reads the result of the previous one.
//
//     in   [7:0] op, [55:8] arg
//     out  [63:0] result
//
//     op 0x00  NOP     read the last result again
//     op 0x01  SYSMON  result = {vccbram, vccaux, vccint, temp}, 16 bits each
//                      -- scbridge's layout, plus VCCBRAM in the top word
//     op 0x05  LINES   result = {ID, 38'd0, scl_lvl, sda_lvl, 8'd0}
//     op 0x09  AUXA    FK33 build (VRM_VAUX) only: result = {vaux13, vaux12, vaux5,
//                      vaux0}, raw 16-bit SYSMON codes (unipolar, 1 V full scale):
//                      VAUX0 = 12 V input (x15.2 divider), VAUX5 / VAUX12 / VAUX13 =
//                      VCCINT / VCCHBM / VCCBRAM current-sense outputs (scale not
//                      documented by SQRL: calibrate on the card)
//     op 0x0A  AUXB    FK33 build only: result = {ID, 16'd0, vaux8, vaux4}: the
//                      LTC3636 temperature outputs (VCCMGT, VCCAUX regulators;
//                      400 K per volt). A build without VRM_VAUX ignores 0x09 and
//                      0x0A and leaves `result` alone, so a host asks for SYSMON
//                      (no ID) first and then AUXB: ID back = this build.
//     op 0x08  PMBUS   arg[2:0] mode, [14:8] addr, [23:16] reg, [39:24] data
//                      (modes 0-7 in pmbus_i2c.sv); result while running has
//                      `busy`, and the finished result replaces it:
//                      {ID, 26'd0, busy, done, timeout, data_ack, reg_ack,
//                       addr_ack, data[15:0]}
//
//   ID = 16'h5642 ("VB") in result[63:48] of LINES and PMBUS, so the host can
//   tell this bridge from scbridge, which never sets those bits.
//
// SAFETY
//   This is a byte pipe. Every limit -- the ceiling, the floor, the step size,
//   the cross-check against SYSMON before anything is written -- lives in
//   host/vrmset.py, where it can be read and reviewed.
`timescale 1ns/1ps

module vrmbridge_top #(
    parameter integer CLK_HZ = 100_000_000
)(
    input  wire clk_p,
    input  wire clk_n,
    inout  wire vrm_scl,
    inout  wire vrm_sda
`ifdef VRM_VAUX
    // SQRL FK33 board sensors on SYSMON's auxiliary analog inputs (bank 66), as
    // SQRL's own example design enables them (OhGodAPet/SQRL_FK33 fk33_example.tcl).
   ,input  wire vaux0_p,  input wire vaux0_n,      // AV33 / AW33  12 V input
    input  wire vaux4_p,  input wire vaux4_n,      // BB34 / BB35  LTC3636 VCCAUX temp
    input  wire vaux5_p,  input wire vaux5_n,      // BC35 / BD35  VCCINT current
    input  wire vaux8_p,  input wire vaux8_n,      // AY34 / BA34  LTC3636 VCCMGT temp
    input  wire vaux12_p, input wire vaux12_n,     // BB36 / BC36  VCCHBM current
    input  wire vaux13_p, input wire vaux13_n      // BE34 / BE35  VCCBRAM current
`endif
);
    localparam [15:0] ID = 16'h5642;

    wire clk_raw, clk;
    IBUFDS u_ibuf (.I(clk_p), .IB(clk_n), .O(clk_raw));
    BUFG   u_bufg (.I(clk_raw), .O(clk));

    // ---- PMBus master -------------------------------------------------------
    logic        go = 1'b0;
    logic [2:0]  mode = '0;
    logic [6:0]  addr = '0;
    logic [7:0]  reg_a = '0;
    logic [15:0] wdata = '0;
    wire         busy, done, addr_ack, reg_ack, data_ack, timeout;
    wire  [15:0] rdata;
    wire         scl_low, sda_low, scl_in, sda_in;

    pmbus_i2c #(.QTR(CLK_HZ / 400_000)) u_i2c (     // ~100 kHz
        .clk(clk), .go(go), .mode(mode), .addr(addr), .reg_a(reg_a),
        .wdata(wdata), .busy(busy), .done(done), .rdata(rdata),
        .addr_ack(addr_ack), .reg_ack(reg_ack), .data_ack(data_ack),
        .timeout(timeout), .scl_low(scl_low), .sda_low(sda_low),
        .scl_in(scl_in), .sda_in(sda_in));

    // Open drain: drive 0 or let go. The board's pull-ups make the high level.
    IOBUF u_scl (.IO(vrm_scl), .I(1'b0), .T(~scl_low), .O(scl_in));
    IOBUF u_sda (.IO(vrm_sda), .I(1'b0), .T(~sda_low), .O(sda_in));

    // ---- SYSMONE4: on-die temperature and supplies --------------------------
    logic [7:0]  daddr = 8'h00;
    logic        den   = 1'b0;
    wire  [15:0] dout;
    wire         drdy;
    logic [15:0] temp_code = '0, vccint_code = '0, vccaux_code = '0, vccbram_code = '0;
    logic [15:0] aux0_code = '0, aux4_code = '0, aux5_code = '0,
                 aux8_code = '0, aux12_code = '0, aux13_code = '0;
    logic [3:0]  which = '0;
    logic [9:0]  sm_div = '0;

    // SYSMON's ADC clock is DCLK / INIT_42[15:8] and must stay under ~5.2 MHz.
    // Divide-by-4 has served the 100 MHz TH53/TH55 builds since they shipped and
    // is kept for them; at the FK33's 200 MHz it ran the ADC at 50 MHz and every
    // conversion read 0, so faster clocks get a divider that lands at <= 5 MHz.
    localparam integer SM_DIV = (CLK_HZ > 100_000_000) ? (CLK_HZ + 4_999_999) / 5_000_000 : 4;
`ifdef VRM_VAUX
    // Continuous sequence (INIT_41 SEQ=0010) over calibration, temp, VCCINT, VCCAUX,
    // VCCBRAM (INIT_48 0x4701) and VAUX 0/4/5/8/12/13 (INIT_49 0x3131), 16-sample
    // averaging (INIT_40 AVG=01) on all but calibration (INIT_4A/4B). Alarms and the
    // over-temperature shutdown stay as they were (enabled). Inputs unipolar.
    localparam [15:0] SM_40 = 16'h1000, SM_41 = 16'h2000, SM_48 = 16'h4701,
                      SM_49 = 16'h3131, SM_4A = 16'h4700, SM_4B = 16'h3131;
    wire [15:0] vauxp = {2'b00, vaux13_p, vaux12_p, 3'b000, vaux8_p, 2'b00, vaux5_p, vaux4_p, 3'b000, vaux0_p};
    wire [15:0] vauxn = {2'b00, vaux13_n, vaux12_n, 3'b000, vaux8_n, 2'b00, vaux5_n, vaux4_n, 3'b000, vaux0_n};
`else
    // Default mode (on-chip sensors only), as every build before VRM_VAUX.
    localparam [15:0] SM_40 = 16'h0000, SM_41 = 16'h0000, SM_48 = 16'h0000,
                      SM_49 = 16'h0000, SM_4A = 16'h0000, SM_4B = 16'h0000;
    wire [15:0] vauxp = 16'd0, vauxn = 16'd0;
`endif
    SYSMONE4 #(.INIT_40(SM_40), .INIT_41(SM_41), .INIT_42({8'(SM_DIV), 8'h00}),
               .INIT_48(SM_48), .INIT_49(SM_49), .INIT_4A(SM_4A), .INIT_4B(SM_4B))
    u_sysmon (
        .DADDR(daddr), .DCLK(clk), .DEN(den), .DI(16'h0000), .DWE(1'b0),
        .DO(dout), .DRDY(drdy), .RESET(1'b0),
        .CONVST(1'b0), .CONVSTCLK(1'b0),
        .VAUXN(vauxn), .VAUXP(vauxp), .VN(1'b0), .VP(1'b0));

`ifdef VRM_VAUX
    localparam [3:0] N_READ = 4'd10;   // 4 on-chip + 6 auxiliary status registers
`else
    localparam [3:0] N_READ = 4'd4;
`endif

    always_ff @(posedge clk) begin
        den <= 1'b0;
        sm_div <= sm_div + 1'b1;
        if (sm_div == 10'd1023) begin
            // status registers: 0x00 temp, 0x01 VCCINT, 0x02 VCCAUX, 0x06 VCCBRAM,
            // and (VRM_VAUX) 0x10 + n for VAUXn
            case (which)
                4'd0: daddr <= 8'h00;
                4'd1: daddr <= 8'h01;
                4'd2: daddr <= 8'h02;
                4'd3: daddr <= 8'h06;
                4'd4: daddr <= 8'h10;
                4'd5: daddr <= 8'h14;
                4'd6: daddr <= 8'h15;
                4'd7: daddr <= 8'h18;
                4'd8: daddr <= 8'h1C;
                default: daddr <= 8'h1D;
            endcase
            den   <= 1'b1;
        end
        if (drdy) begin
            case (which)
                4'd0: temp_code    <= dout;
                4'd1: vccint_code  <= dout;
                4'd2: vccaux_code  <= dout;
                4'd3: vccbram_code <= dout;
                4'd4: aux0_code    <= dout;
                4'd5: aux4_code    <= dout;
                4'd6: aux5_code    <= dout;
                4'd7: aux8_code    <= dout;
                4'd8: aux12_code   <= dout;
                default: aux13_code <= dout;
            endcase
            which <= (which == N_READ - 4'd1) ? 4'd0 : which + 4'd1;
        end
    end

    // ---- BSCAN command register (same scheme as scbridge) -------------------
    wire bscan_sel, bscan_drck, bscan_shift, bscan_tdi, bscan_capture, bscan_update;
    wire bscan_tdo;

    BSCANE2 #(.JTAG_CHAIN(1)) u_bscan (
        .CAPTURE(bscan_capture), .DRCK(bscan_drck), .RESET(),
        .RUNTEST(), .SEL(bscan_sel), .SHIFT(bscan_shift),
        .TCK(), .TDI(bscan_tdi), .TMS(), .UPDATE(bscan_update),
        .TDO(bscan_tdo));

    logic [63:0] sh = '0;
    logic [63:0] result = '0;
    assign bscan_tdo = sh[0];

    always_ff @(posedge bscan_drck) begin
        if (bscan_sel) begin
            if (bscan_capture)     sh <= result;
            else if (bscan_shift)  sh <= {bscan_tdi, sh[63:1]};
        end
    end

    // DRCK is gated and static in Update-DR: sample `sh` from clk (see scbridge).
    logic [2:0] upd_sync = '0;
    always_ff @(posedge clk) upd_sync <= {upd_sync[1:0], bscan_update & bscan_sel};
    wire cmd_stb = upd_sync[1] & ~upd_sync[2];

    wire [7:0]  op  = sh[7:0];
    wire [47:0] arg = sh[55:8];

    logic [1:0] lvl_s = 2'b11;
    always_ff @(posedge clk) lvl_s <= {scl_in, sda_in};

    wire [63:0] pm_status = {ID, 26'd0, busy, 1'b1, timeout, data_ack,
                             reg_ack, addr_ack, rdata};

    always_ff @(posedge clk) begin
        go <= 1'b0;
        // A finished transaction lands first, so a NOP poll arriving on the
        // same cycle cannot swallow it; a real command below still wins.
        if (done) result <= pm_status;
        if (cmd_stb) begin
            case (op)
                8'h01: result <= {vccbram_code, vccaux_code, vccint_code, temp_code};
                8'h05: result <= {ID, 38'd0, lvl_s, 8'd0};
`ifdef VRM_VAUX
                8'h09: result <= {aux13_code, aux12_code, aux5_code, aux0_code};
                8'h0A: result <= {ID, 16'd0, aux8_code, aux4_code};
`endif
                8'h08: if (!busy) begin
                    mode  <= arg[2:0];
                    addr  <= arg[14:8];
                    reg_a <= arg[23:16];
                    wdata <= arg[39:24];
                    go    <= 1'b1;
                    result <= {ID, 26'd0, 1'b1, 21'd0};      // busy, not done
                end
                default: ;
            endcase
        end
    end
endmodule

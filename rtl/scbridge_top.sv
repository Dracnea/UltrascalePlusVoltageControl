// scbridge -- sc_uart behind a JTAG BSCAN register, plus SYSMONE4.
//
// This is the standalone probe: load it, set a rail, load whatever you actually
// wanted to run. The rail setpoint persists across reconfiguration, which is
// what makes that sequence work.
//
// If you would rather set rails WITHOUT a bitstream swap -- from a design that
// is already running -- instantiate `sc_uart` directly behind your own control
// channel instead of using this top. That module is the reusable part; this
// file is only the JTAG wrapper around it.
//
// PROTOCOL
//   One BSCANE2 user register (USER1), 64 bits, shifted LSB first. Every shift
//   writes a command and reads the result of the previous one, so a read is
//   "send the command, then send a NOP and take what comes back".
//
//     in   [7:0] op, [39:8] arg
//     out  [63:0] result
//
//     op 0x00  NOP      read the last result again
//     op 0x01  SYSMON   result = {vccaux[15:0], vccint[15:0], temp[15:0]}
//     op 0x02  CONFIG   arg[15:0] baud divisor, [16] rx pin, [17] rx parity,
//                       [18] tx pin, [19] tx parity, [20] tx parity odd;
//                       also empties both queues
//     op 0x03  QUEUE    arg[7:0] appended to the transmit queue (64 deep)
//     op 0x04  RX       pop one byte: result[8] valid, result[7:0] byte,
//                       result[24:16] still waiting
//     op 0x05  LINES    [15:0] tx edges, [31:16] rx edges, [32] txd level,
//                       [33] rxd level, [37:34] gpio
//     op 0x06  BURST    send the queue back-to-back at line rate
//     op 0x07  CLEAR    empty the transmit queue and the receive FIFO
//
//   Popping one byte at a time means a reply of any length can be read; there
//   is no fixed capture window to overflow.
//
// SAFETY
//   SYSMON, LINES and RX are pure reads. Nothing here can flash controller
//   firmware: this is a byte pipe, and the command that enters the bootloader
//   is simply never sent by the host tool.
`timescale 1ns/1ps

module scbridge_top #(
    parameter integer CLK_HZ = 100_000_000
)(
    input  wire       clk_p,
    input  wire       clk_n,
    output wire       hbm_cattrip,   // must be driven low or the card powers off
    inout  wire       sc_txd,
    inout  wire       sc_rxd,
    input  wire [3:0] sc_gpio
);
    assign hbm_cattrip = 1'b0;

    wire clk_raw, clk;
    IBUFDS u_ibuf (.I(clk_p), .IB(clk_n), .O(clk_raw));
    BUFG   u_bufg (.I(clk_raw), .O(clk));

    // ---- the controller's UART ----------------------------------------------
    logic        cfg_pulse = 1'b0, q_pulse = 1'b0;
    logic        burst_pulse = 1'b0, clr_pulse = 1'b0, rx_pop = 1'b0;
    logic [15:0] cfg_baud_div = CLK_HZ / 115200;
    logic        cfg_rx_pin = 1'b1, cfg_rx_par = 1'b1;
    logic        cfg_tx_pin = 1'b0, cfg_tx_par = 1'b1, cfg_tx_odd = 1'b0;
    logic [7:0]  q_byte = '0;

    wire        rx_valid;
    wire [7:0]  rx_byte, rx_count;
    wire        tx_busy, txd_level, rxd_level;
    wire [6:0]  tx_count;
    wire [15:0] txd_edges, rxd_edges;

    sc_uart #(.CLK_HZ(CLK_HZ)) u_uart (
        .clk(clk), .sc_txd(sc_txd), .sc_rxd(sc_rxd),
        .cfg_pulse(cfg_pulse), .cfg_baud_div(cfg_baud_div),
        .cfg_rx_pin(cfg_rx_pin), .cfg_rx_par(cfg_rx_par),
        .cfg_tx_pin(cfg_tx_pin), .cfg_tx_par(cfg_tx_par), .cfg_tx_odd(cfg_tx_odd),
        .q_pulse(q_pulse), .q_byte(q_byte),
        .burst_pulse(burst_pulse), .clr_pulse(clr_pulse),
        .rx_pop(rx_pop), .rx_valid(rx_valid), .rx_byte(rx_byte), .rx_count(rx_count),
        .tx_busy_o(tx_busy), .tx_count(tx_count),
        .txd_level(txd_level), .rxd_level(rxd_level),
        .txd_edges_o(txd_edges), .rxd_edges_o(rxd_edges));

    // ---- SYSMONE4: on-die temperature and supplies --------------------------
    logic [7:0]  daddr = 8'h00;
    logic        den   = 1'b0;
    wire  [15:0] dout;
    wire         drdy;
    logic [15:0] temp_code = '0, vccint_code = '0, vccaux_code = '0;
    logic [1:0]  which = '0;
    logic [9:0]  sm_div = '0;

    SYSMONE4 #(.INIT_40(16'h0000), .INIT_41(16'h0000), .INIT_42(16'h0400))
    u_sysmon (
        .DADDR(daddr), .DCLK(clk), .DEN(den), .DI(16'h0000), .DWE(1'b0),
        .DO(dout), .DRDY(drdy), .RESET(1'b0),
        .CONVST(1'b0), .CONVSTCLK(1'b0),
        .VAUXN(16'd0), .VAUXP(16'd0), .VN(1'b0), .VP(1'b0));

    always_ff @(posedge clk) begin
        den <= 1'b0;
        sm_div <= sm_div + 1'b1;
        if (sm_div == 10'd1023) begin
            daddr <= (which == 2'd0) ? 8'h00 : (which == 2'd1) ? 8'h01 : 8'h02;
            den   <= 1'b1;
        end
        if (drdy) begin
            case (which)
                2'd0: temp_code   <= dout;
                2'd1: vccint_code <= dout;
                default: vccaux_code <= dout;
            endcase
            which <= (which == 2'd2) ? 2'd0 : which + 1'b1;
        end
    end

    // ---- BSCAN command register --------------------------------------------
    wire bscan_sel, bscan_drck, bscan_shift, bscan_tdi, bscan_capture, bscan_update;
    wire bscan_tdo;

    BSCANE2 #(.JTAG_CHAIN(1)) u_bscan (
        .CAPTURE(bscan_capture), .DRCK(bscan_drck), .RESET(),
        .RUNTEST(), .SEL(bscan_sel), .SHIFT(bscan_shift),
        .TCK(), .TDI(bscan_tdi), .TMS(), .UPDATE(bscan_update),
        .TDO(bscan_tdo));

    logic [63:0] sh = '0;          // the shift register, in the DRCK domain
    logic [63:0] result = '0;      // loaded on CAPTURE
    assign bscan_tdo = sh[0];

    always_ff @(posedge bscan_drck) begin
        if (bscan_sel) begin
            if (bscan_capture)     sh <= result;
            else if (bscan_shift)  sh <= {bscan_tdi, sh[63:1]};
        end
    end

    // UPDATE -> a pulse in the clk domain.
    //
    // Do NOT try to latch the command off DRCK. DRCK is a GATED TCK: it runs in
    // Capture-DR and Shift-DR and is static in Update-DR, so a register clocked
    // by it on UPDATE never captures anything and every command reads back as
    // zero. Sample the shift register from the clk domain instead -- `sh` is
    // stable once shifting has stopped, which is exactly when UPDATE asserts.
    logic [2:0] upd_sync = '0;
    always_ff @(posedge clk) upd_sync <= {upd_sync[1:0], bscan_update & bscan_sel};
    wire cmd_stb = upd_sync[1] & ~upd_sync[2];

    wire [7:0]  op  = sh[7:0];
    wire [31:0] arg = sh[39:8];

    always_ff @(posedge clk) begin
        cfg_pulse   <= 1'b0;
        q_pulse     <= 1'b0;
        burst_pulse <= 1'b0;
        clr_pulse   <= 1'b0;
        rx_pop      <= 1'b0;
        if (cmd_stb) begin
            case (op)
                8'h01: result <= {16'd0, vccaux_code, vccint_code, temp_code};
                8'h02: begin
                    cfg_baud_div <= arg[15:0];
                    cfg_rx_pin   <= arg[16]; cfg_rx_par <= arg[17];
                    cfg_tx_pin   <= arg[18]; cfg_tx_par <= arg[19];
                    cfg_tx_odd   <= arg[20];
                    cfg_pulse    <= 1'b1;
                    result       <= {48'd0, arg[15:0]};
                end
                8'h03: begin
                    q_byte  <= arg[7:0];
                    q_pulse <= 1'b1;
                    result  <= {56'd0, 1'b0, tx_count};
                end
                8'h04: begin
                    rx_pop <= 1'b1;              // answer lands on the next NOP
                    result <= 64'd0;
                end
                8'h05: result <= {26'd0, sc_gpio, rxd_level, txd_level,
                                  rxd_edges, txd_edges};
                8'h06: begin
                    burst_pulse <= 1'b1;
                    result      <= {55'd0, tx_busy, 1'b0, tx_count};
                end
                8'h07: begin
                    clr_pulse <= 1'b1;
                    result    <= 64'd0;
                end
                default: ;   // NOP: leave the previous result in place
            endcase
        end else if (rx_valid) begin
            // A pop answers one cycle late, so it lands here rather than in the
            // command arm above.
            result <= {39'd0, rx_count, 7'd0, 1'b1, rx_byte};
        end
    end
endmodule

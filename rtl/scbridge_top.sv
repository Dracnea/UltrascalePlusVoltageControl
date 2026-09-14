// scbridge -- a UART bridge from JTAG to an UltraScale+ card's satellite
// controller, so the host can set the board's voltage rails.
//
// Written from scratch for this project. The satellite controller on these
// cards owns VCCINT/VCCBRAM/VCCMEM and is reachable only from the fabric, on a
// plain UART; this puts that UART on a JTAG BSCAN register so a host script can
// speak to it. SYSMONE4 is included so the result can be read back on the die,
// independently of whatever the controller claims.
//
// PROTOCOL
//   One BSCANE2 user register, 64 bits, shifted LSB first. Every shift both
//   writes a command and reads the result of the previous one:
//
//     in   [7:0] op, [39:8] arg
//     out  [63:0] result
//
//     op 0x00  NOP      read the last result again
//     op 0x01  SYSMON   result = {vccaux[15:0], vccint[15:0], temp[15:0]}
//     op 0x02  CONFIG   arg[15:0] baud divisor (clk/baud)
//                       arg[16]   rx pin   (0 = sc_txd, 1 = sc_rxd)
//                       arg[17]   rx parity expected
//                       arg[18]   tx pin
//                       arg[19]   tx parity enable
//                       arg[20]   tx parity odd
//                       also clears the receive FIFO
//     op 0x03  QUEUE    arg[7:0] appended to the transmit queue (64 deep)
//     op 0x06  BURST    send the queue back-to-back at line rate
//     op 0x07  CLEAR    empty the transmit queue
//     op 0x04  RX       pop one byte: result[8] valid, result[7:0] byte,
//                       result[24:16] bytes still queued
//     op 0x05  LINES    result[15:0]  tx edge count
//                       result[31:16] rx edge count
//                       result[32]    sc_txd level, [33] sc_rxd level
//                       result[37:34] sc_gpio
//
//   Popping one byte at a time means a reply of any length can be read; there
//   is no fixed capture window to overflow.
//
// SAFETY
//   Both UART pins are tri-stated and only driven while a byte is going out, on
//   the pin CONFIG selected. Nothing here can flash satellite-controller
//   firmware: this is a byte pipe, and the command that enters the bootloader
//   is simply never sent by the host tool.
`timescale 1ns/1ps

module scbridge_top #(
    parameter integer CLK_HZ = 100_000_000
)(
    input  wire       clk_p,
    input  wire       clk_n,
    output wire       hbm_cattrip,   // must be driven low or the card powers off
    inout  wire       sc_txd,        // FPGA -> satellite controller
    inout  wire       sc_rxd,        // satellite controller -> FPGA
    input  wire [3:0] sc_gpio
);
    assign hbm_cattrip = 1'b0;

    wire clk_raw, clk;
    IBUFDS u_ibuf (.I(clk_p), .IB(clk_n), .O(clk_raw));
    BUFG   u_bufg (.I(clk_raw), .O(clk));

    // ---- configuration ------------------------------------------------------
    logic [15:0] baud_div = CLK_HZ / 115200;
    logic        rx_pin   = 1'b1;
    logic        rx_par   = 1'b1;      // 8E1 by default: these controllers use it
    logic        tx_pin   = 1'b0;
    logic        tx_par   = 1'b1;
    logic        tx_odd   = 1'b0;

    // ---- pins ---------------------------------------------------------------
    logic tx_bit = 1'b1, tx_oe = 1'b0;
    wire  txd_in, rxd_in;
    IOBUF u_txd (.I(tx_bit), .T(~(tx_oe & ~tx_pin)), .O(txd_in), .IO(sc_txd));
    IOBUF u_rxd (.I(tx_bit), .T(~(tx_oe &  tx_pin)), .O(rxd_in), .IO(sc_rxd));
    wire rx_in = rx_pin ? rxd_in : txd_in;

    // ---- edge counters, for finding the line and the bit rate ---------------
    logic [2:0]  t_sync = '1, r_sync = '1;
    logic [15:0] t_edges = '0, r_edges = '0;
    always_ff @(posedge clk) begin
        t_sync <= {t_sync[1:0], txd_in};
        r_sync <= {r_sync[1:0], rxd_in};
        if (t_sync[2] ^ t_sync[1]) t_edges <= t_edges + 1'b1;
        if (r_sync[2] ^ r_sync[1]) r_edges <= r_edges + 1'b1;
    end

    // ---- transmitter, fed from a queue --------------------------------------
    //
    // A queue is not a luxury. One JTAG round-trip through Vivado costs tens of
    // milliseconds, so transmitting a byte per round-trip spreads a nine-byte
    // frame over seconds and the controller times out between characters. Queue
    // the whole frame, then burst it back-to-back at line rate.
    logic [7:0] txq [0:63];
    logic [6:0] txq_wr = '0, txq_rd = '0;
    logic       txq_clr = 1'b0, burst = 1'b0;

    logic [3:0]  tx_state = '0;
    logic [15:0] tx_cnt = '0;
    logic [7:0]  tx_data = '0;
    logic        tx_busy = 1'b0;
    wire         tx_more = (txq_rd != txq_wr);

    always_ff @(posedge clk) begin
        if (txq_clr) begin
            txq_rd <= '0; tx_busy <= 1'b0; tx_oe <= 1'b0; tx_bit <= 1'b1;
        end else if (!tx_busy) begin
            if (burst && tx_more) begin                 // fetch and start
                tx_data  <= txq[txq_rd[5:0]];
                txq_rd   <= txq_rd + 1'b1;
                tx_busy  <= 1'b1;
                tx_state <= 4'd0;
                tx_cnt   <= '0;
                tx_oe    <= 1'b1;
                tx_bit   <= 1'b0;                       // start bit
            end else begin
                tx_oe  <= 1'b0;
                tx_bit <= 1'b1;
            end
        end else if (tx_cnt == baud_div - 1) begin
            tx_cnt   <= '0;
            tx_state <= tx_state + 1'b1;
            case (tx_state)
                4'd0,4'd1,4'd2,4'd3,4'd4,4'd5,4'd6: tx_bit <= tx_data[tx_state];
                4'd7: tx_bit <= tx_data[7];
                4'd8: tx_bit <= tx_par ? (^tx_data ^ tx_odd) : 1'b1;
                4'd9: tx_bit <= 1'b1;                   // stop (or 2nd stop, 8N1)
                default: tx_busy <= 1'b0;               // next byte, or idle
            endcase
            if (!tx_par && tx_state == 4'd9) tx_busy <= 1'b0;
        end else begin
            tx_cnt <= tx_cnt + 1'b1;
        end
    end

    // ---- receiver, into a 256-byte FIFO ------------------------------------
    logic [7:0]  fifo [0:255];
    logic [7:0]  fifo_wr = '0, fifo_rd = '0;
    wire  [7:0]  fifo_n = fifo_wr - fifo_rd;
    logic        fifo_clr = 1'b0;

    logic [3:0]  rx_state = '0;
    logic [15:0] rx_cnt = '0;
    logic [7:0]  rx_data = '0;
    logic [2:0]  rx_sync = '1;

    // State 1 is the MIDDLE OF THE START BIT and must not be sampled -- it is
    // there to confirm the edge was real and to line the counter up so that
    // states 2..9 land in the middle of each data bit. Sampling at state 1
    // shifts the start bit in as data and every byte comes out garbage, which
    // looks exactly like a wrong baud rate.
    always_ff @(posedge clk) begin
        rx_sync <= {rx_sync[1:0], rx_in};
        if (fifo_clr) begin
            fifo_wr <= '0; rx_state <= '0; rx_cnt <= '0;
        end else if (rx_state == 4'd0) begin
            if (!rx_sync[2]) begin                  // falling edge: start bit
                rx_state <= 4'd1;
                rx_cnt   <= '0;
            end
        end else if (rx_cnt == ((rx_state == 4'd1) ? (baud_div >> 1) : baud_div) - 1) begin
            rx_cnt <= '0;
            case (rx_state)
                4'd1: rx_state <= rx_sync[2] ? 4'd0 : 4'd2;   // glitch? abandon
                4'd2, 4'd3, 4'd4, 4'd5, 4'd6, 4'd7, 4'd8, 4'd9: begin
                    rx_data  <= {rx_sync[2], rx_data[7:1]};   // LSB first
                    rx_state <= rx_state + 1'b1;
                end
                4'd10: begin                        // parity if expected, else stop
                    if (rx_par) begin
                        rx_state <= 4'd11;
                    end else begin
                        fifo[fifo_wr] <= rx_data;
                        fifo_wr  <= fifo_wr + 1'b1;
                        rx_state <= 4'd0;
                    end
                end
                default: begin                      // stop bit
                    fifo[fifo_wr] <= rx_data;
                    fifo_wr  <= fifo_wr + 1'b1;
                    rx_state <= 4'd0;
                end
            endcase
        end else begin
            rx_cnt <= rx_cnt + 1'b1;
        end
    end

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
    // Do NOT try to latch the command off DRCK here. DRCK is a GATED TCK: it
    // runs in Capture-DR and Shift-DR and is static in Update-DR, so a register
    // clocked by it on UPDATE never captures anything, and every command reads
    // back as zero. Sample the shift register from the clk domain instead --
    // `sh` is stable once shifting has stopped, which is exactly when UPDATE is
    // asserted.
    logic [2:0] upd_sync = '0;
    always_ff @(posedge clk) upd_sync <= {upd_sync[1:0], bscan_update & bscan_sel};
    wire cmd_stb = upd_sync[1] & ~upd_sync[2];

    wire [7:0]  op  = sh[7:0];
    wire [31:0] arg = sh[39:8];

    always_ff @(posedge clk) begin
        txq_clr  <= 1'b0;
        fifo_clr <= 1'b0;
        if (burst && !tx_more && !tx_busy) burst <= 1'b0;
        if (cmd_stb) begin
            case (op)
                8'h01: result <= {16'd0, vccaux_code, vccint_code, temp_code};
                8'h02: begin
                    fifo_rd  <= '0;              // the receiver owns fifo_wr
                    baud_div <= arg[15:0];
                    rx_pin   <= arg[16]; rx_par <= arg[17];
                    tx_pin   <= arg[18]; tx_par <= arg[19]; tx_odd <= arg[20];
                    fifo_clr <= 1'b1;
                    result   <= {48'd0, arg[15:0]};
                end
                8'h03: begin                          // QUEUE one byte
                    if (txq_wr[6] == 1'b0) begin
                        txq[txq_wr[5:0]] <= arg[7:0];
                        txq_wr <= txq_wr + 1'b1;
                    end
                    result <= {56'd0, 1'd0, txq_wr + 7'd1};
                end
                8'h06: begin                          // BURST the queue
                    burst  <= 1'b1;
                    result <= {55'd0, tx_busy, 1'd0, txq_wr};
                end
                8'h07: begin                          // CLEAR the queue
                    txq_wr  <= '0;
                    txq_clr <= 1'b1;
                    burst   <= 1'b0;
                    result  <= 64'd0;
                end
                8'h04: begin
                    if (fifo_n != 0) begin
                        result  <= {39'd0, fifo_n - 8'd1, 7'd0, 1'b1, fifo[fifo_rd]};
                        fifo_rd <= fifo_rd + 1'b1;
                    end else begin
                        result <= 64'd0;
                    end
                end
                8'h05: result <= {26'd0, sc_gpio, r_sync[2], t_sync[2],
                                  r_edges, t_edges};
                default: ;   // NOP: leave the previous result in place
            endcase
        end
    end
endmodule

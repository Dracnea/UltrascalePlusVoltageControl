// pmbus_i2c -- a minimal open-drain I2C master for PMBus regulator access.
//
// One transaction per `go` strobe. The mode selects the shape:
//
//   0 QUICK       S addr+W P                          presence probe
//   1 READ_BYTE   S addr+W reg Sr addr+R [d0]~A P
//   2 WRITE_BYTE  S addr+W reg d0 P
//   3 READ_WORD   S addr+W reg Sr addr+R [d0]A [d1]~A P   (PMBus words are LE)
//   4 WRITE_WORD  S addr+W reg d0 d1 P
//   5 SEND_BYTE   S addr+W reg P                       e.g. CLEAR_FAULTS
//
// A NACK on the address ends the transaction at once with a STOP, and
// addr_ack stays 0: that is how a missing regulator, or the wrong pins, shows.
//
// Bus speed is CLK_HZ / (4 * QTR); the default is ~100 kHz from 100 MHz.
// Clock stretching is honoured: in every phase where SCL is released, the
// phase timer does not run until SCL actually reads high. A slave that holds
// SCL for longer than STRETCH_MAX cycles aborts the transaction with `timeout`.
`timescale 1ns/1ps

module pmbus_i2c #(
    parameter integer QTR         = 250,          // clk cycles per quarter bit
    parameter integer STRETCH_MAX = 2_500_000     // 25 ms at 100 MHz
)(
    input  wire        clk,
    input  wire        go,
    input  wire [2:0]  mode,
    input  wire [6:0]  addr,
    input  wire [7:0]  reg_a,
    input  wire [15:0] wdata,
    output logic       busy = 1'b0,
    output logic       done = 1'b0,        // one-cycle pulse at the end
    output logic [15:0] rdata = '0,
    output logic       addr_ack = 1'b0,
    output logic       reg_ack = 1'b0,
    output logic       data_ack = 1'b0,    // every written data byte ACKed
    output logic       timeout = 1'b0,
    // open drain: *_low = 1 pulls the line low, 0 releases it
    output logic       scl_low = 1'b0,
    output logic       sda_low = 1'b0,
    input  wire        scl_in,
    input  wire        sda_in
);
    // ---- primitives: each is four quarter-bit phases of (SCL, SDA) ----------
    localparam [2:0] P_START = 3'd0, P_RESTART = 3'd1, P_BIT = 3'd2, P_STOP = 3'd3;

    // Byte-level steps of a transaction.
    localparam [3:0] S_START = 4'd0, S_ADDRW = 4'd1, S_REG = 4'd2, S_D0 = 4'd3,
                     S_D1 = 4'd4, S_RESTART = 4'd5, S_ADDRR = 4'd6, S_R0 = 4'd7,
                     S_R1 = 4'd8, S_STOP = 4'd9, S_END = 4'd10;

    logic [2:0]  m = '0;
    logic [6:0]  a = '0;
    logic [7:0]  r = '0;
    logic [15:0] wd = '0;

    logic [3:0]  step = S_END;
    logic [2:0]  prim = P_STOP;
    logic [1:0]  ph = '0;
    logic [3:0]  bitn = '0;          // 0..7 data, 8 = ack slot
    logic [7:0]  tx = '0, rx = '0;
    logic        rx_ack_out = 1'b0;  // the ack we send after a read byte (0 = ACK)
    logic [$clog2(QTR+1)-1:0]          qcnt = '0;
    logic [$clog2(STRETCH_MAX+1)-1:0]  scnt = '0;
    logic        active = 1'b0;

    // Synchronise the bus inputs.
    logic [1:0] scl_s = 2'b11, sda_s = 2'b11;
    always_ff @(posedge clk) begin
        scl_s <= {scl_s[0], scl_in};
        sda_s <= {sda_s[0], sda_in};
    end
    wire scl_hi = scl_s[1];
    wire sda_hi = sda_s[1];

    // Released-line levels for the current primitive and phase (1 = released).
    logic scl_rel, sda_rel;
    wire  bitval = (bitn == 4'd8) ? ((step == S_R0 || step == S_R1) ? rx_ack_out : 1'b1)
                 : ((step == S_R0 || step == S_R1) ? 1'b1 : tx[7 - bitn[2:0]]);
    always_comb begin
        case (prim)
            P_START:   begin scl_rel = (ph != 2'd3);  sda_rel = (ph == 2'd0); end
            P_RESTART: begin scl_rel = (ph == 2'd1 || ph == 2'd2); sda_rel = (ph <= 2'd1); end
            P_BIT:     begin scl_rel = (ph == 2'd1 || ph == 2'd2); sda_rel = bitval; end
            default:   begin scl_rel = (ph != 2'd0);  sda_rel = (ph >= 2'd2); end   // STOP
        endcase
    end

    // What the next step is, given the mode and the step just finished.
    function automatic [3:0] next_step(input [3:0] s, input [2:0] md);
        case (s)
            S_START:   next_step = S_ADDRW;
            S_ADDRW:   next_step = (md == 3'd0) ? S_STOP : S_REG;
            S_REG:     next_step = (md == 3'd1 || md == 3'd3) ? S_RESTART
                                 : (md == 3'd2 || md == 3'd4) ? S_D0 : S_STOP;
            S_D0:      next_step = (md == 3'd4) ? S_D1 : S_STOP;
            S_D1:      next_step = S_STOP;
            S_RESTART: next_step = S_ADDRR;
            S_ADDRR:   next_step = S_R0;
            S_R0:      next_step = (md == 3'd3) ? S_R1 : S_STOP;
            S_R1:      next_step = S_STOP;
            default:   next_step = S_END;
        endcase
    endfunction

    task automatic begin_step(input [3:0] s);
        step <= s;
        ph   <= 2'd0;
        bitn <= '0;
        case (s)
            S_START:   prim <= P_START;
            S_RESTART: prim <= P_RESTART;
            S_STOP:    prim <= P_STOP;
            default:   prim <= P_BIT;
        endcase
        case (s)
            S_ADDRW: tx <= {a, 1'b0};
            S_ADDRR: tx <= {a, 1'b1};
            S_REG:   tx <= r;
            S_D0:    tx <= wd[7:0];
            S_D1:    tx <= wd[15:8];
            default: tx <= 8'h00;
        endcase
        // ACK the low byte of a word read, NACK the last byte read.
        rx_ack_out <= (s == S_R0 && m == 3'd3) ? 1'b0 : 1'b1;
    endtask

    always_ff @(posedge clk) begin
        done <= 1'b0;
        if (!active) begin
            scl_low <= 1'b0;
            sda_low <= 1'b0;
            if (go) begin
                m <= mode; a <= addr; r <= reg_a; wd <= wdata;
                addr_ack <= 1'b0; reg_ack <= 1'b0; data_ack <= 1'b0;
                timeout <= 1'b0; rdata <= '0;
                active <= 1'b1; busy <= 1'b1;
                qcnt <= '0; scnt <= '0;
                begin_step(S_START);
            end
        end else begin
            scl_low <= ~scl_rel;
            sda_low <= ~sda_rel;
            // Clock stretching: while SCL is released but still low, hold the
            // phase timer. A slave holding it too long aborts the transaction.
            if (scl_rel && !scl_hi && qcnt == '0 && scnt != STRETCH_MAX) begin
                scnt <= scnt + 1'b1;
            end else if (scl_rel && !scl_hi && scnt == STRETCH_MAX) begin
                timeout <= 1'b1;
                active  <= 1'b0; busy <= 1'b0; done <= 1'b1;
            end else if (qcnt != QTR - 1) begin
                qcnt <= qcnt + 1'b1;
                // Sample in the middle of the SCL-high window of a bit.
                if (prim == P_BIT && ph == 2'd2 && qcnt == QTR / 2) begin
                    if (bitn == 4'd8) begin
                        // A read needs both address phases ACKed.
                        if (step == S_ADDRW)                    addr_ack <= ~sda_hi;
                        if (step == S_ADDRR)                    addr_ack <= addr_ack & ~sda_hi;
                        if (step == S_REG)                      reg_ack  <= ~sda_hi;
                        if (step == S_D0)                       data_ack <= ~sda_hi;
                        if (step == S_D1)                       data_ack <= data_ack & ~sda_hi;
                    end else begin
                        rx <= {rx[6:0], sda_hi};
                    end
                end
            end else begin
                qcnt <= '0;
                scnt <= '0;
                if (ph != 2'd3) begin
                    ph <= ph + 1'b1;
                end else if (prim == P_BIT && bitn != 4'd8) begin
                    ph   <= 2'd0;
                    bitn <= bitn + 1'b1;
                end else begin
                    // Step complete.
                    if (step == S_R0) rdata[7:0]  <= rx;
                    if (step == S_R1) rdata[15:8] <= rx;
                    if (step == S_STOP) begin
                        active <= 1'b0; busy <= 1'b0; done <= 1'b1;
                    end else if ((step == S_ADDRW || step == S_ADDRR) && !addr_ack) begin
                        begin_step(S_STOP);                 // nobody home
                    end else begin
                        begin_step(next_step(step, m));
                    end
                end
            end
        end
    end
endmodule

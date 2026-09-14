// sc_uart -- the satellite controller's UART, as a module you can drop into any
// design.
//
// WHY IT IS A MODULE. The controller owns the board's voltage rails and answers
// only on a fabric UART pair. If that link lives solely in a probe bitstream,
// changing a rail costs a bitstream swap -- you cannot set a voltage while your
// real design is running. Instantiated behind whatever control channel a design
// already has, the design itself can set rails and read them back, and stay
// running while it does.
//
// CHARACTER FORMAT IS 8E1, AND THAT IS THE CONTROLLER'S, NOT A DEFAULT. 8N1
// draws garbage and 8O1 draws silence -- both look exactly like a dead
// controller, which is how most attempts at this end. Parity is a port only so
// a board that genuinely differs can be tried.
//
// QUEUE THEN BURST, ALWAYS. A transmitter that sends one byte per host
// round-trip spreads a frame over as long as the round-trip takes, and the
// controller times out between characters. On a JTAG host that is fatal: a
// round-trip is tens of milliseconds.
//
// HOST INTERFACE -- one-cycle strobes on clk:
//   cfg_pulse   with cfg_*     baud divisor, pin selection, parity; clears both
//                              queues, because stale bytes outlive a reconfigure
//   q_pulse     with q_byte    append one byte to the transmit queue (64 deep)
//   burst_pulse                shift the queue out back-to-back, then empty it
//   clr_pulse                  empty the transmit queue and the receive FIFO
//   rx_pop      ->             rx_valid / rx_byte one cycle later; rx_count is
//                              how many are still waiting
//
// The pins are tri-stated and driven only while a byte is going out, on the pin
// cfg_tx_pin selects. Nothing here can flash controller firmware.
`timescale 1ns/1ps

module sc_uart #(
    parameter integer CLK_HZ = 100_000_000
)(
    input  wire        clk,

    inout  wire        sc_txd,          // one of these is the controller's RX,
    inout  wire        sc_rxd,          // the other its TX; cfg_* picks which

    input  wire        cfg_pulse,
    input  wire [15:0] cfg_baud_div,    // CLK_HZ / baud
    input  wire        cfg_rx_pin,      // 0 = listen on sc_txd, 1 = on sc_rxd
    input  wire        cfg_rx_par,
    input  wire        cfg_tx_pin,
    input  wire        cfg_tx_par,
    input  wire        cfg_tx_odd,

    input  wire        q_pulse,
    input  wire [7:0]  q_byte,
    input  wire        burst_pulse,
    input  wire        clr_pulse,

    input  wire        rx_pop,
    output logic       rx_valid = 1'b0,
    output logic [7:0] rx_byte  = '0,
    output wire [7:0]  rx_count,

    output wire        tx_busy_o,
    output wire [6:0]  tx_count,
    output wire        txd_level,
    output wire        rxd_level,
    output wire [15:0] txd_edges_o,
    output wire [15:0] rxd_edges_o
);
    // ---- configuration ------------------------------------------------------
    localparam [15:0] DIV_115200 = CLK_HZ / 115200;
    logic [15:0] baud_div = DIV_115200;
    logic        rx_pin = 1'b1, rx_par = 1'b1;
    logic        tx_pin = 1'b0, tx_par = 1'b1, tx_odd = 1'b0;

    wire cfg_or_clr = cfg_pulse | clr_pulse;

    always_ff @(posedge clk) if (cfg_pulse) begin
        baud_div <= (cfg_baud_div == 16'd0) ? DIV_115200 : cfg_baud_div;
        rx_pin   <= cfg_rx_pin;  rx_par <= cfg_rx_par;
        tx_pin   <= cfg_tx_pin;  tx_par <= cfg_tx_par;  tx_odd <= cfg_tx_odd;
    end

    // ---- pins ---------------------------------------------------------------
    logic tx_bit = 1'b1, tx_oe = 1'b0;
    wire  txd_in, rxd_in;
    IOBUF u_txd (.I(tx_bit), .T(~(tx_oe & ~tx_pin)), .O(txd_in), .IO(sc_txd));
    IOBUF u_rxd (.I(tx_bit), .T(~(tx_oe &  tx_pin)), .O(rxd_in), .IO(sc_rxd));
    wire  rx_in = rx_pin ? rxd_in : txd_in;

    // ---- edge counters: finding the live line, and the bit rate --------------
    logic [2:0]  t_sync = '1, r_sync = '1;
    logic [15:0] t_edges = '0, r_edges = '0;
    always_ff @(posedge clk) begin
        t_sync <= {t_sync[1:0], txd_in};
        r_sync <= {r_sync[1:0], rxd_in};
        if (t_sync[2] ^ t_sync[1]) t_edges <= t_edges + 1'b1;
        if (r_sync[2] ^ r_sync[1]) r_edges <= r_edges + 1'b1;
    end

    // ---- transmit queue -----------------------------------------------------
    logic [7:0] txq [0:63];
    logic [6:0] txq_wr = '0, txq_rd = '0;
    logic       burst = 1'b0;
    wire        tx_more = (txq_rd != txq_wr);

    logic [3:0]  tx_state = '0;
    logic [15:0] tx_cnt = '0;
    logic [7:0]  tx_data = '0;
    logic        tx_busy = 1'b0;

    always_ff @(posedge clk) begin
        if (cfg_or_clr) begin
            txq_wr <= '0; txq_rd <= '0; burst <= 1'b0;
            tx_busy <= 1'b0; tx_oe <= 1'b0; tx_bit <= 1'b1;
        end else begin
            if (q_pulse && !txq_wr[6]) begin
                txq[txq_wr[5:0]] <= q_byte;
                txq_wr <= txq_wr + 1'b1;
            end
            if (burst_pulse) burst <= 1'b1;
            else if (burst && !tx_more && !tx_busy) burst <= 1'b0;

            if (!tx_busy) begin
                if (burst && tx_more) begin
                    tx_data  <= txq[txq_rd[5:0]];
                    txq_rd   <= txq_rd + 1'b1;
                    tx_busy  <= 1'b1;
                    tx_state <= 4'd0;
                    tx_cnt   <= '0;
                    tx_oe    <= 1'b1;
                    tx_bit   <= 1'b0;                    // start bit
                end else begin
                    tx_oe <= 1'b0; tx_bit <= 1'b1;
                end
            end else if (tx_cnt == baud_div - 1) begin
                tx_cnt   <= '0;
                tx_state <= tx_state + 1'b1;
                case (tx_state)
                    4'd0,4'd1,4'd2,4'd3,4'd4,4'd5,4'd6: tx_bit <= tx_data[tx_state];
                    4'd7: tx_bit <= tx_data[7];
                    4'd8: tx_bit <= tx_par ? (^tx_data ^ tx_odd) : 1'b1;
                    4'd9: tx_bit <= 1'b1;                // stop
                    default: tx_busy <= 1'b0;            // next byte, or idle
                endcase
                if (!tx_par && tx_state == 4'd9) tx_busy <= 1'b0;
            end else begin
                tx_cnt <= tx_cnt + 1'b1;
            end
        end
    end

    // ---- receiver, into a 256-byte FIFO -------------------------------------
    //
    // State 1 is the MIDDLE OF THE START BIT and must not be sampled. It
    // confirms the edge was real and lines the counter up so states 2..9 land
    // in the middle of each data bit. Sampling at state 1 shifts the start bit
    // in as data and every byte comes out garbage -- indistinguishable from a
    // wrong baud rate, and a long afternoon to find.
    logic [7:0]  fifo [0:255];
    logic [7:0]  fifo_wr = '0, fifo_rd = '0;
    logic [3:0]  rx_state = '0;
    logic [15:0] rx_cnt = '0;
    logic [7:0]  rx_data = '0;
    logic [2:0]  rx_sync = '1;

    always_ff @(posedge clk) begin
        rx_sync <= {rx_sync[1:0], rx_in};
        if (cfg_or_clr) begin
            fifo_wr <= '0; rx_state <= '0; rx_cnt <= '0;
        end else if (rx_state == 4'd0) begin
            if (!rx_sync[2]) begin rx_state <= 4'd1; rx_cnt <= '0; end
        end else if (rx_cnt == ((rx_state == 4'd1) ? (baud_div >> 1) : baud_div) - 1) begin
            rx_cnt <= '0;
            case (rx_state)
                4'd1: rx_state <= rx_sync[2] ? 4'd0 : 4'd2;      // glitch? abandon
                4'd2,4'd3,4'd4,4'd5,4'd6,4'd7,4'd8,4'd9: begin
                    rx_data  <= {rx_sync[2], rx_data[7:1]};      // LSB first
                    rx_state <= rx_state + 1'b1;
                end
                4'd10: begin
                    if (rx_par) rx_state <= 4'd11;               // parity bit
                    else begin
                        fifo[fifo_wr] <= rx_data;
                        fifo_wr <= fifo_wr + 1'b1;
                        rx_state <= 4'd0;
                    end
                end
                default: begin                                    // stop bit
                    fifo[fifo_wr] <= rx_data;
                    fifo_wr <= fifo_wr + 1'b1;
                    rx_state <= 4'd0;
                end
            endcase
        end else begin
            rx_cnt <= rx_cnt + 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        rx_valid <= 1'b0;
        if (cfg_or_clr) begin
            fifo_rd <= '0;
        end else if (rx_pop && (fifo_wr != fifo_rd)) begin
            rx_byte  <= fifo[fifo_rd];
            rx_valid <= 1'b1;
            fifo_rd  <= fifo_rd + 1'b1;
        end
    end

    assign rx_count    = fifo_wr - fifo_rd;
    assign tx_count    = txq_wr;
    assign tx_busy_o   = tx_busy;
    assign txd_level   = t_sync[2];
    assign rxd_level   = r_sync[2];
    assign txd_edges_o = t_edges;
    assign rxd_edges_o = r_edges;
endmodule

// u55c_board_top -- the Alveo U55C's "parking" and pin-check image.
//
//   - the six QSFP LEDs driven OFF (left unused, UNUSEDPIN PULLUP lights them);
//   - hbm_cattrip (BE45) driven LOW, or the satellite controller reads an HBM
//     over-temperature and powers the card off;
//   - the 100 MHz SYSCLK3 (BK43/BK44) counted, and the count readable over JTAG
//     through BSCANE2 USER4: a 64-bit capture of {count[31:0], MAGIC}. Two reads
//     a known time apart measure the board clock, which proves the clock pins.
//
// The QSFP cages' own control pins (ResetL, LPMode, ModSelL) do not reach the
// fabric on this board -- the satellite controller owns them -- and the GTYs are
// left uninstantiated, so Vivado powers them down. The LEDs are the only QSFP
// hardware a bitstream controls. Nothing here talks to the satellite controller.
module u55c_board_top (
    input  wire       clk_p,
    input  wire       clk_n,
    output wire       hbm_cattrip,
    output wire [1:0] qsfp_led_act,
    output wire [1:0] qsfp_led_stat_g,
    output wire [1:0] qsfp_led_stat_y
);
    localparam logic [31:0] MAGIC = 32'h55C0_B0A1;   // "U55C board image, rev 1"

    assign hbm_cattrip     = 1'b0;
    assign qsfp_led_act    = 2'b00;
    assign qsfp_led_stat_g = 2'b00;
    assign qsfp_led_stat_y = 2'b00;

    wire clk_ibuf, clk;
    IBUFDS u_ibuf (.I(clk_p), .IB(clk_n), .O(clk_ibuf));
    BUFG   u_bufg (.I(clk_ibuf), .O(clk));

    logic [31:0] count = '0;
    always_ff @(posedge clk) count <= count + 1'b1;

    // USER4 data register. CAPTURE samples a gray-coded copy of the count, so a
    // capture that lands mid-increment is off by one, never garbage.
    wire capture, drck, shift, tdi, sel, tck;
    wire tdo;
    BSCANE2 #(.JTAG_CHAIN(4)) u_bscan (
        .CAPTURE(capture), .DRCK(drck), .RESET(), .RUNTEST(), .SEL(sel), .SHIFT(shift),
        .TCK(tck), .TDI(tdi), .TMS(), .UPDATE(), .TDO(tdo));

    logic [31:0] gray = '0;
    always_ff @(posedge clk) gray <= count ^ (count >> 1);
    (* ASYNC_REG = "TRUE" *) logic [31:0] g1 = '0, g2 = '0;
    always_ff @(posedge tck) begin g1 <= gray; g2 <= g1; end

    logic [63:0] sr = '0;
    always_ff @(posedge tck) begin
        if (sel && capture)    sr <= {g2, MAGIC};
        else if (sel && shift) sr <= {tdi, sr[63:1]};
    end
    assign tdo = sr[0];
endmodule

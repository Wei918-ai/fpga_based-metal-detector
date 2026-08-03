`timescale 1ns / 1ps
module dac_clk_fwd (
    input  wire clk,        // Connected to FCLK_CLK0 (100MHz)
    output wire dac_clk     // Forwarded to M19 pin
);
    ODDR #(
        .DDR_CLK_EDGE("OPPOSITE_EDGE"),
        .INIT(1'b0),
        .SRTYPE("SYNC")
    ) u_oddr (
        .Q  (dac_clk),
        .C  (clk),
        .CE (1'b1),
        .D1 (1'b1),
        .D2 (1'b0),
        .R  (1'b0),
        .S  (1'b0)
    );
endmodule

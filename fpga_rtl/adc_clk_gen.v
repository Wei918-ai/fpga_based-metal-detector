`timescale 1ns / 1ps
module adc_clk_gen (
    input  wire clk100,       // FCLK_CLK0, 100MHz
    output reg  adc_clk = 1'b0
);
    // 100MHz / (2 * 50) = 1MHz
    localparam DIV = 49;
    reg [5:0] cnt = 6'd0;     // Bit-width check: 49 < 64, [5:0] is sufficient

    always @(posedge clk100) begin
        if (cnt == DIV) begin
            cnt     <= 6'd0;
            adc_clk <= ~adc_clk;
        end else begin
            cnt <= cnt + 1'b1;
        end
    end
endmodule

//adc_clk_gen

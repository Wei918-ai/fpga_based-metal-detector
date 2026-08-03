//-----------------------------------------------------------------------------
// adc9240_rx.v -- Dual-Channel AD9240 14-bit Parallel CMOS Receiver -> AXI-Stream (32-bit)
//
// Features:
//    1. Synchronously latches two 14-bit ADC channel inputs on the rising edge of shared adc_clk.
//    2. Packs data into 28-bit words and writes to an asynchronous FIFO, crossing from
//       adc_clk domain to m_axis_aclk domain (100MHz).
//    3. Left-shifts each channel by 2 bits to form 16-bit words, combining into a 32-bit AXI-Stream:
//         m_axis_tdata[15:0]  = ch1 data (lower 16 bits)
//         m_axis_tdata[31:16] = ch2 data (upper 16 bits)
//
// Downstream Parsing (PC side):
//    4 bytes per sample point: first 2 bytes = ch1, last 2 bytes = ch2,
//    both straight binary unsigned 16-bit integers.
//    numpy: data32 = np.frombuffer(bytes, dtype="<u4")
//           ch1 = (data32 & 0xFFFF).astype("<u2")
//           ch2 = (data32 >> 16).astype("<u2")
//
// Notes:
//    - Both ADCs share the same adc_clk to guarantee sampling synchronization (paired channels per instant).
//    - FIFO depth of 512 is sufficient to absorb rate differences (10 MSPS input, 100 MHz output).
//    - AD9240 pipeline delay is 3 clock cycles, aligned across both channels (ensured by shared clock source).
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
module adc9240_rx (
    // ---- ADC Side (from sensor board, dual channels sharing clock) ----
    input  wire        adc_clk,        // Shared sampling clock, data valid on rising edge
    input  wire [13:0] adc1_in,        // Channel 1 data: BIT14(MSB)..BIT1(LSB)
    input  wire [13:0] adc2_in,        // Channel 2 data
    // ---- AXI-Stream Master Interface (to axis_tlast_gen, 32-bit version) ----
    input  wire        m_axis_aclk,    // System clock (100MHz)
    input  wire        m_axis_aresetn, // Active-low reset
    output wire [31:0] m_axis_tdata,   // {ch2, ch1} 16 bits each
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready
);
    // ------------------------------------------------------------
    // 1. ADC Clock Domain: Latch both channels simultaneously (first-stage registers near pin capture)
    // ------------------------------------------------------------
    reg [13:0] adc1_r = 14'd0;
    reg [13:0] adc2_r = 14'd0;
    always @(posedge adc_clk) begin
        adc1_r <= adc1_in;
        adc2_r <= adc2_in;
    end

    // Pack into 28 bits: {ch2, ch1} entered into FIFO together to prevent channel misalignment
    wire [27:0] pair_in = {adc2_r, adc1_r};

    // ------------------------------------------------------------
    // 2. Asynchronous FIFO: adc_clk domain -> m_axis_aclk domain, moving 28-bit paired data per transfer
    // ------------------------------------------------------------
    wire        fifo_full;
    wire        fifo_empty;
    wire [27:0] fifo_dout;
    wire        fifo_wr_rst_busy;
    wire        fifo_rd_rst_busy;
    xpm_fifo_async #(
        .FIFO_MEMORY_TYPE    ("block"),
        .FIFO_WRITE_DEPTH    (512),
        .WRITE_DATA_WIDTH    (28),
        .READ_DATA_WIDTH     (28),
        .READ_MODE           ("fwft"),
        .FIFO_READ_LATENCY   (0),
        .CDC_SYNC_STAGES     (3),
        .RELATED_CLOCKS      (0)
    ) u_cdc_fifo (
        .rst           (~m_axis_aresetn),
        .wr_clk        (adc_clk),
        .wr_en         (~fifo_full & ~fifo_wr_rst_busy),
        .din           (pair_in),
        .full          (fifo_full),
        .wr_rst_busy   (fifo_wr_rst_busy),
        .rd_clk        (m_axis_aclk),
        .rd_en         (m_axis_tvalid & m_axis_tready),
        .dout          (fifo_dout),
        .empty         (fifo_empty),
        .rd_rst_busy   (fifo_rd_rst_busy),
        .sleep         (1'b0),
        .injectsbiterr (1'b0),
        .injectdbiterr (1'b0)
    );

    // ------------------------------------------------------------
    // 3. AXI-Stream Output: Left-shift both 14-bit channels by 2 bits to pad into 16 bits, concatenated into 32 bits
    //    {ch2[13:0], 2'b00, ch1[13:0], 2'b00} = {ch2<<2, ch1<<2}
    // ------------------------------------------------------------
    wire [15:0] ch1_16 = {fifo_dout[13:0],  2'b00};
    wire [15:0] ch2_16 = {fifo_dout[27:14], 2'b00};
    assign m_axis_tdata  = {ch2_16, ch1_16};
    assign m_axis_tvalid = ~fifo_empty & ~fifo_rd_rst_busy;
endmodule

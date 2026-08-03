module axis_tlast_gen #(
    parameter DATA_WIDTH = 32,
    parameter BURST_LEN  = 1024   // Number of sample points between TLAST insertions, adjustable as needed
)(
    input  wire                     aclk,
    input  wire                     aresetn,

    // Input from DDS
    input  wire [DATA_WIDTH-1:0]    s_axis_tdata,
    input  wire                     s_axis_tvalid,
    output wire                     s_axis_tready,

    // Output to FIFO/DMA
    output wire [DATA_WIDTH-1:0]    m_axis_tdata,
    output wire                     m_axis_tvalid,
    output wire                     m_axis_tlast,
    input  wire                     m_axis_tready
);

    reg [$clog2(BURST_LEN)-1:0] count;

    assign m_axis_tdata  = s_axis_tdata;
    assign m_axis_tvalid = s_axis_tvalid;
    assign s_axis_tready = m_axis_tready;
    assign m_axis_tlast  = (count == BURST_LEN - 1);

    always @(posedge aclk) begin
        if (!aresetn) begin
            count <= 0;
        end else if (s_axis_tvalid && m_axis_tready) begin
            if (count == BURST_LEN - 1)
                count <= 0;
            else
                count <= count + 1;
        end
    end

endmodule

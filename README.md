# FPGA-Based Metal Detection & Complex Impedance DAQ System

A high-speed, dual-channel data acquisition and complex impedance measurement system implemented on the ZedBoard (Xilinx Zynq-7020). 

The system features onboard DDS excitation generation, 1 MSPS dual-channel ADC sampling, a standalone ring-buffer DMA pipeline, and zero-loss UDP Ethernet streaming with hardware-assisted ARQ.

---

## 1. System Architecture & Specifications

### 1.1 Development Environment
* FPGA / SoC Platform: Digilent ZedBoard (XC7Z020-CLG484-1)
* Design Suite: AMD Xilinx Vivado 2025.2 / Vitis 2025.2 (SDT flow)
* Operating System: Standalone (Baremetal C Firmware)
* Network Stack: lwIP v2.2.0 (UDP Protocol)
* Host Software: Python 3.10+ (NumPy / Matplotlib)

### 1.2 Network Configuration
* **ZedBoard IP**: `192.168.1.100`
* **Host PC IP**: `192.168.1.101`
* **Port / Physical Layer**: Port `5001` via 100 Mbps Direct Ethernet Cable

---

## 2. Hardware Signal Chain & Clocking

### 2.1 Signal Path Schematic
```text
[ DDS PINC Reg ]
       │
       ▼
  [ DDS Core ] ──> [ xlslice ] ──> [ MSB Flip ] ──> [ AD9764 DAC ] ──> [ Op-Amp ] ──> [ Tx Coil ]
                                                                                         │
  [ PC Host ] <── [ lwIP UDP ] <── [ AXI DMA ] <── [ FIFO ] <── [ adc9240_rx ] <── [ AD9240 ADC ]
                                (DDR Memory)                   (Ch1: Volt / Ch2: Curr)
### 2.2 Clock Tree Configuration
* **FCLK_CLK0**: `100 MHz` (PS Reference Clock)
* **`adc_clk`**: `1 MHz` — Generated in PL logic via counter divider (`DIV=49`), forwarded to physical pin `E20` via an `ODDR` primitive (`adc_clk_fwd.v`).
* **`dac_clk`**: `100 MHz` — Direct 100 MHz clock forwarded to physical pin `M19` via an `ODDR` primitive.

### 2.3 Data Packet & Frame Specifications
* **Sampling Rate**: 1 MSPS synchronous dual-channel sampling.
* **Sample Format**: 4 Bytes per sample tuple:
  * `[15:0]`: Channel 1 (Voltage) — 14-bit left-aligned (`<< 2`), unsigned integer.
  * `[31:16]`: Channel 2 (Current) — 14-bit left-aligned (`<< 2`), unsigned integer.
* **UDP Frame Format**: 1028 Bytes total payload per packet.
  * `Bytes [0:3]`: 32-bit Unsigned Packet Sequence Number (`u32 seq`).
  * `Bytes [4:1027]`: 256 Sample Tuples (256 × 4 Bytes = 1024 Bytes).
* **Burst Configuration**: 1024 AXI DMA beats per burst transfer (maps to 4 UDP packets per DMA interrupt cycle).

---

## 3. Core Software & Firmware Design

### 3.1 PS Pipeline Architecture (`dds_stream.c` v5)
The PS side runs under Xilinx Standalone OS with a non-blocking 8-buffer ring queue (`RING8`) to maximize throughput without RTOS scheduler overhead:
* **Ring Buffer Allocation**: 8 dedicated memory buffers in DDR. Fill Index (`FillIdx`) tracking DMA write location and Send Index (`SendIdx`) tracking CPU network dispatch.
* **Non-Blocking DMA Pump (`dma_pump()`)**: Manual polling function inserted into execution loops. Checks DMA completion status, rotates target buffers instantly upon DMA completion, and yields execution back within microseconds.
* **Cache Coherency Management**: Enforces explicit `Xil_DCacheInvalidateRange` calls on newly acquired DMA buffers prior to packetization to prevent reading stale L1/L2 cache lines.

### 3.2 Hardware-Assisted ARQ Protocol
* History Ring Buffer**: Retains a rolling window of 2048 transmitted packets in DDR.
* NACK Handling**: Accepts 4-byte NACK commands sent from the host PC over UDP (`0x44445301`). Triggers instant retransmission of requested sequence IDs.
* Performance**: Achieved verified **LOST = 0** over continuous streams exceeding $3.63 \times 10^8$ samples.

### 3.3 Dynamic Excitation Tuning
* Host PC broadcasts 12-byte UDP command frames containing a 4-byte header and target frequency in Hertz.
* PS firmware extracts target frequency, calculates Phase Increment Value ($PINC$), and writes directly to the AXI GPIO mapped at base address `0x41200000` to update the DDS core in real time.

---

## 4. Repository Layout

```text
.
├── board_ps/               # PS Firmware Source Code (Vitis Standalone C)
│   ├── main.c              # Entry point, static IP setup & main loop
│   ├── dds_stream.c        # Ring buffer, DMA pump & ARQ implementation
│   ├── dds_stream.h        # Buffer, register, and hardware mapping headers
│   └── CMakeLists.txt      # Vitis SDT build definition
├── fpga_rtl/               # RTL Top Modules & IP Definitions (Vivado)
│   ├── adc9240_rx.v        # AD9240 receiver & 28-bit atomic data packer
│   ├── adc_clk_gen.v       # Clock divider for 1 MHz ADC clock
│   ├── dac_clk_fwd.v       # ODDR-based DAC clock forwarding logic
│   └── fake_adc.v          # Dual-channel synthetic data generator for testbench
├── constraints/            # XDC Physical Pin Constraints
│   ├── sensor_full.xdc     # Complete pin assignments for full hardware stack
│   └── dac_only.xdc        # Minimal constraints for DAC loopback test
└── pc_daq/                 # Host PC Python Scripts
    ├── pc_waveform_dual.py # Real-time dual-channel oscilloscope display
    ├── check_continuity_dual.py # Packet continuity and zero-loss checker
    ├── set_freq.py         # Command-line tool for remote DDS frequency control
    └── pc_recorder.py      # Binary data logging script

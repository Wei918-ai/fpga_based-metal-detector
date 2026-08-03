# FPGA-Based Metal Detection & Complex Impedance DAQ System

A high-speed, dual-channel data acquisition and complex impedance measurement system implemented on the **ZedBoard (Xilinx Zynq-7020)**.

The system features onboard DDS excitation generation, **1 MSPS** dual-channel ADC sampling, a standalone ring-buffer DMA pipeline, and zero-loss UDP Ethernet streaming with hardware-assisted ARQ.

---

# 1. System Architecture & Specifications

## 1.1 Development Environment

- **FPGA / SoC Platform:** Digilent ZedBoard (XC7Z020-CLG484-1)
- **Design Suite:** AMD Xilinx Vivado 2025.2 / Vitis 2025.2 (SDT Flow)
- **Operating System:** Standalone (Bare-Metal C Firmware)
- **Network Stack:** lwIP v2.2.0 (UDP)
- **Host Software:** Python 3.10+ (NumPy / Matplotlib)

## 1.2 Network Configuration

- **ZedBoard IP:** `192.168.1.100`
- **Host PC IP:** `192.168.1.101`
- **Port:** `5001`
- **Physical Layer:** 100 Mbps Direct Ethernet Connection

---

# 2. Hardware Signal Chain & Clocking

## 2.1 Signal Path

```text
Transmit (Excitation)
[DDS PINC Reg] → [DDS Core] → [xlslice] → [MSB Flip] → [AD9764 DAC] → [Op-Amp] → [Tx Coil]

Receive (Acquisition)
[AD9240 ADC (Ch1: Voltage / Ch2: Current)] → [adc9240_rx] → [FIFO] → [AXI DMA (DDR)] → [lwIP UDP] → [PC Host]
```

## 2.2 Clock Tree

- **FCLK_CLK0:** `100 MHz` (PS Reference Clock)
- **ADC Clock (`adc_clk`):**
  - Frequency: `1 MHz`
  - Generated inside PL using a counter divider (`DIV = 49`)
  - Forwarded to physical pin **E20** through an `ODDR` primitive (`adc_clk_fwd.v`)
- **DAC Clock (`dac_clk`):**
  - Frequency: `100 MHz`
  - Directly forwarded to physical pin **M19** through an `ODDR` primitive

## 2.3 Data Format

### Sample Format

| Bits | Description |
|------|-------------|
| `[15:0]` | Channel 1 (Voltage), 14-bit left-aligned (`<<2`) |
| `[31:16]` | Channel 2 (Current), 14-bit left-aligned (`<<2`) |

### UDP Packet Format

| Bytes | Description |
|-------|-------------|
| 0–3 | 32-bit Packet Sequence Number (`u32 seq`) |
| 4–1027 | 256 Sample Tuples (1024 Bytes) |

### DMA Burst Configuration

- **Burst Length:** 1024 AXI DMA beats
- **Equivalent:** 4 UDP packets per DMA completion interrupt

---

# 3. Core Software & Firmware Design

## 3.1 PS Pipeline Architecture (`dds_stream.c` v5)

The PS firmware runs under the Xilinx Standalone environment using a non-blocking **8-buffer ring queue (RING8)**.

### Ring Buffer

- Eight dedicated DDR buffers
- **FillIdx** tracks DMA write location
- **SendIdx** tracks packet transmission order

### Non-Blocking DMA Pump

`dma_pump()` continuously polls DMA status.

Upon DMA completion, it:

- Rotates the target buffer
- Immediately starts the next DMA transfer
- Returns execution within a few microseconds

### Cache Coherency

Before packetization, the firmware explicitly calls:

```c
Xil_DCacheInvalidateRange();
```

to prevent stale cache lines from being transmitted.

## 3.2 Hardware-Assisted ARQ

To guarantee zero packet loss, the firmware implements a lightweight ARQ mechanism.

### Features

- Rolling history buffer containing the most recent **2048 packets**
- 4-byte UDP NACK command (`0x44445301`)
- Immediate retransmission of missing sequence IDs

### Verified Performance

- **LOST = 0**
- Continuous streaming over 3.63 × 10^8 samples without packet loss

## 3.3 Dynamic DDS Frequency Control

The Host PC sends a **12-byte UDP command packet** containing:

- 4-byte command header
- Target frequency (Hz)

The PS firmware:

1. Parses the target frequency.
2. Computes the DDS Phase Increment (`PINC`).
3. Writes the new value to the AXI GPIO register (`0x41200000`).

This enables real-time DDS frequency tuning without interrupting data acquisition.

---

# 4. Repository Layout

```text
.
├── board_ps/
│   ├── main.c
│   ├── dds_stream.c
│   ├── dds_stream.h
│   └── CMakeLists.txt
│
├── fpga_rtl/
│   ├── adc9240_rx.v
│   ├── adc_clk_gen.v
│   ├── axis_tlast_gen.v
│   └── dac_clk_fwd.v
│
├── constraints/
│   └── sensor_full.xdc
│
└── pc_daq/
    ├── pc_waveform_dual.py
    └── set_freq.py
```

| Directory | Description |
|-----------|-------------|
| **board_ps/** | Bare-metal firmware running on the PS |
| **fpga_rtl/** | FPGA RTL source code |
| **constraints/** | Vivado XDC constraint files |
| **pc_daq/** | Python utilities for acquisition, visualization and control |

---

# 5. Build & Deployment

## 5.1 Hardware Setup

1. Set **JP9** and **JP10** to **SD Boot Mode (3V3)**.
2. Copy **BOOT.BIN** to a FAT32-formatted SD card.
3. Insert the SD card into the ZedBoard.
4. Connect the Host PC via Ethernet.
5. Configure the Host PC:

```text
IP Address : 192.168.1.101
Subnet Mask: 255.255.255.0
```

6. Connect the UART cable.

```text
Baud Rate: 115200
```

## 5.2 Boot Verification

```text
Board IP: 192.168.1.100
[dds_stream] ready(v5+RING8+ARQ+CMD)
[dds_stream] gpio readback: 0x00000863
```

## 5.3 Host PC Operation

Launch the real-time oscilloscope:

```bash
python pc_waveform_dual.py
```

Set DDS output frequency (example: 75 kHz):

```bash
python set_freq.py 75000
```


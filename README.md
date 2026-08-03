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

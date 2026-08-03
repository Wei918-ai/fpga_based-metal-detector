/*
 * dds_stream.c (v5 - Ring Multi-Buffering / True Concurrent Capture & Transmit)
 * Continuous Capture + UDP + ARQ + Frequency Tuning
 *
 * Flaw in v4 (discovered during testing):
 *   v4 initiated DMA only once "before sending a buffer", completing capture in ~0.51 ms.
 *   After that, DMA remained idle for the entire transmission period (~5 ms), causing
 *   ADC data to flood the 512-depth FIFO, leading to a structural overflow.
 *
 * v5 Fix - True Pipeline:
 *   - 8 buffer slots arranged as a ring queue.
 *   - Inside the send loop: "Check DMA after sending each packet". As soon as one buffer
 *     is filled, immediately switch to the next buffer and restart DMA.
 *   - DMA is virtually never idle (only microsecond-level delays during restart instructions,
 *     which are easily absorbed by the FIFO).
 *   - Transmitter fetches filled buffers sequentially from the ring queue for transmission.
 *
 *   Production 2 MSPS (32.8 Mbps) < Transmission (measured 36 Mbps) -> Queue remains mostly empty,
 *   achieving steady-state operation.
 *
 * Protocol remains 100% compatible with v3/v4; transparent to PC end.
 */

#include <string.h>

#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "sleep.h"

#include "lwip/udp.h"
#include "lwip/pbuf.h"
#include "lwip/ip_addr.h"

/* ---------------- Configurable Parameters ---------------- */
#define PC_IP_0           192
#define PC_IP_1           168
#define PC_IP_2           1
#define PC_IP_3           101
#define PC_PORT           5001
#define LOCAL_PORT        5001

#define BURST_SAMPLES     1024
#define PKT_SAMPLES       256
#define PKT_GAP_US        215             /* Measured 36 Mbps, do not modify */
#define TIMEOUT_LOOPS     10000000

#define HISTORY_PKTS      2048

#define NUM_BUFS          8               /* Ring buffer count (power of 2): 8 buffers ≈ 4ms buffer depth */

#define CMD_MAGIC         0x44445301u
#define DDS_CLK_HZ        100000000ULL
#define FREQ_MIN_HZ       1
#define FREQ_MAX_HZ       40000000
/* --------------------------------------------------------- */

#define BURST_BYTES       (BURST_SAMPLES * 4)
#define PKT_DATA_BYTES    (PKT_SAMPLES * 4)
#define PKT_TOTAL_BYTES   (4 + PKT_DATA_BYTES)
#define PKTS_PER_BURST    (BURST_SAMPLES / PKT_SAMPLES)
#define HISTORY_MASK      (HISTORY_PKTS - 1)
#define BUF_MASK          (NUM_BUFS - 1)

static XAxiDma        AxiDma;
static struct udp_pcb *Pcb;
static ip_addr_t      PcAddr;
static u32            Seq;
static u32            ErrCnt;
static u32            RetxCnt;
static u32            QueueDrop;         /* Diagnostics: Buffer drop count due to ring queue overflow */
static u32            MaxDepth;          /* Diagnostics: Historical maximum queue occupancy depth */

/* [v5] Ring Multi-Buffer */
static u8  Bufs[NUM_BUFS][BURST_BYTES] __attribute__((aligned(64)));
static u32 FillIdx;                       /* Index of buffer currently being filled by DMA */
static u32 SendIdx;                       /* Index of oldest filled buffer awaiting transmission */
static u32 FullCnt;                       /* Number of currently filled buffers */
static int DmaRunning;

static u8 History[HISTORY_PKTS][PKT_TOTAL_BYTES];

static err_t send_raw(const u8 *data)
{
    struct pbuf *pb = pbuf_alloc(PBUF_TRANSPORT, PKT_TOTAL_BYTES, PBUF_RAM);
    if (!pb) return ERR_MEM;
    memcpy(pb->payload, data, PKT_TOTAL_BYTES);
    err_t err = udp_sendto(Pcb, pb, &PcAddr, PC_PORT);
    pbuf_free(pb);
    return err;
}

/* PC Downlink: 12-byte frequency tuning / 4-byte NACK (same as v3/v4) */
static void nack_recv_cb(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                         const ip_addr_t *addr, u16_t port)
{
    (void)arg; (void)pcb; (void)addr; (void)port;
    if (!p) return;

    if (p->len == 12) {
        const u8 *b = (const u8 *)p->payload;
        u32 magic = (u32)b[0] | ((u32)b[1] << 8) |
                    ((u32)b[2] << 16) | ((u32)b[3] << 24);
        if (magic == CMD_MAGIC) {
            u32 freq = (u32)b[4] | ((u32)b[5] << 8) |
                       ((u32)b[6] << 16) | ((u32)b[7] << 24);
            if (freq >= FREQ_MIN_HZ && freq <= FREQ_MAX_HZ) {
                u32 pinc = (u32)(((u64)freq << 32) / DDS_CLK_HZ);
                xil_printf("[cmd] freq=%u pinc=%u -> 0x%08x\r\n",
                           freq, pinc, (u32)XPAR_AXI_GPIO_0_BASEADDR);
                Xil_Out32(XPAR_AXI_GPIO_0_BASEADDR + 0x0, pinc);
                xil_printf("[cmd] write done, DDS -> %u Hz\r\n", freq);
            } else {
                xil_printf("[cmd] freq %u out of range\r\n", freq);
            }
            pbuf_free(p);
            return;
        }
    }

    if (p->len >= 4) {
        const u8 *b = (const u8 *)p->payload;
        u32 want = (u32)b[0] | ((u32)b[1] << 8) |
                   ((u32)b[2] << 16) | ((u32)b[3] << 24);
        u32 age = Seq - want;
        if (age > 0 && age <= HISTORY_PKTS) {
            u8 *slot = History[want & HISTORY_MASK];
            u32 stored = (u32)slot[0] | ((u32)slot[1] << 8) |
                         ((u32)slot[2] << 16) | ((u32)slot[3] << 24);
            if (stored == want) {
                if (send_raw(slot) == ERR_OK) {
                    RetxCnt++;
                    if (RetxCnt % 10 == 1)
                        xil_printf("[dds_stream] retx seq=%u (retx=%u)\r\n",
                                   want, RetxCnt);
                }
            }
        }
    }
    pbuf_free(p);
}

static int start_dma(u32 idx)
{
    if (XAxiDma_SimpleTransfer(&AxiDma, (UINTPTR)Bufs[idx & BUF_MASK],
                               BURST_BYTES,
                               XAXIDMA_DEVICE_TO_DMA) != XST_SUCCESS) {
        if (++ErrCnt % 1000 == 1)
            xil_printf("[dds_stream] DMA start err (cnt=%d)\r\n", ErrCnt);
        return -1;
    }
    return 0;
}

/*
 * [v5 Core] Pump: Non-blocking DMA polling/servicing routine.
 * Called once per packet transmission in the send loop: checks if DMA has filled a buffer,
 * marks it ready, and immediately switches to restart DMA on the next buffer slot.
 * This keeps DMA running continuously throughout the transmission phase.
 */
static void dma_pump(void)
{
    if (!DmaRunning) {
        /* Do not start new capture when queue is full (drop full buffer, increment counter)
           to give transmission loop room to catch up */
        if (FullCnt >= NUM_BUFS - 1) {
            QueueDrop++;
            return;
        }
        DmaRunning = (start_dma(FillIdx) == 0);
        return;
    }
    if (!XAxiDma_Busy(&AxiDma, XAXIDMA_DEVICE_TO_DMA)) {
        /* Buffer capture complete: mark as full buffer */
        Xil_DCacheInvalidateRange((UINTPTR)Bufs[FillIdx & BUF_MASK],
                                  BURST_BYTES);
        FillIdx++;
        FullCnt++;
        if (FullCnt > MaxDepth) MaxDepth = FullCnt;
        DmaRunning = 0;
        /* Immediately attempt to start capturing into the next buffer (if queue is not full) */
        if (FullCnt < NUM_BUFS - 1)
            DmaRunning = (start_dma(FillIdx) == 0);
        else
            QueueDrop++;               /* Queue full: rely on hardware FIFO to hold data temporarily */
    }
}

int dds_stream_init(void)
{
    XAxiDma_Config *cfg = XAxiDma_LookupConfig(XPAR_XAXIDMA_0_BASEADDR);
    if (!cfg) { xil_printf("[dds_stream] FAIL: no DMA config\r\n"); return -1; }
    if (XAxiDma_CfgInitialize(&AxiDma, cfg) != XST_SUCCESS) {
        xil_printf("[dds_stream] FAIL: DMA init\r\n"); return -1;
    }
    XAxiDma_IntrDisable(&AxiDma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrDisable(&AxiDma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DMA_TO_DEVICE);

    Pcb = udp_new();
    if (!Pcb) { xil_printf("[dds_stream] FAIL: udp_new\r\n"); return -1; }
    if (udp_bind(Pcb, IP_ADDR_ANY, LOCAL_PORT) != ERR_OK) {
        xil_printf("[dds_stream] FAIL: udp_bind\r\n"); return -1;
    }
    udp_recv(Pcb, nack_recv_cb, NULL);
    IP4_ADDR(&PcAddr, PC_IP_0, PC_IP_1, PC_IP_2, PC_IP_3);

    Seq = 0; ErrCnt = 0; RetxCnt = 0;
    QueueDrop = 0; MaxDepth = 0;
    FillIdx = 0; SendIdx = 0; FullCnt = 0;

    DmaRunning = (start_dma(FillIdx) == 0);

    xil_printf("[dds_stream] ready(v5+RING%d+ARQ+CMD): to %d.%d.%d.%d:%d, "
               "history=%d, gpio=0x%08x\r\n",
               NUM_BUFS, PC_IP_0, PC_IP_1, PC_IP_2, PC_IP_3, PC_PORT,
               HISTORY_PKTS, (u32)XPAR_AXI_GPIO_0_BASEADDR);
    xil_printf("[dds_stream] gpio readback: 0x%08x\r\n",
               Xil_In32(XPAR_AXI_GPIO_0_BASEADDR));
    return 0;
}

void dds_stream_poll(void)
{
    if (!Pcb) return;

    dma_pump();

    /* When no filled buffers are available, wait briefly (continuing to pump DMA during wait) */
    if (FullCnt == 0) {
        u32 guard = 0;
        while (FullCnt == 0) {
            dma_pump();
            if (++guard > TIMEOUT_LOOPS) {
                xil_printf("[dds_stream] no data timeout\r\n");
                return;
            }
        }
    }

    /* Transmit the oldest filled buffer; interleave dma_pump between packet sends — True Concurrent Transmit & Capture */
    u8 *buf = Bufs[SendIdx & BUF_MASK];
    for (u32 p = 0; p < PKTS_PER_BURST; p++) {
        u8 *slot = History[Seq & HISTORY_MASK];
        slot[0] = (u8)(Seq);
        slot[1] = (u8)(Seq >> 8);
        slot[2] = (u8)(Seq >> 16);
        slot[3] = (u8)(Seq >> 24);
        memcpy(slot + 4, buf + p * PKT_DATA_BYTES, PKT_DATA_BYTES);

        if (send_raw(slot) == ERR_OK) {
            Seq++;
            if (Seq % 100000 == 0) {
                xil_printf("[s] %u q=%u d=%u\r\n",
                           Seq, QueueDrop, MaxDepth);
                MaxDepth = FullCnt;
            }
        } else if (++ErrCnt % 1000 == 1) {
            xil_printf("[dds_stream] send err (cnt=%d)\r\n", ErrCnt);
        }

        dma_pump();                    /* [v5 Core] Pump DMA during inter-packet gap */
        usleep(PKT_GAP_US);
        dma_pump();
    }

    SendIdx++;
    FullCnt--;
}

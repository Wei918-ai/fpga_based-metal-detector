#!/usr/bin/env python3
"""
pc_waveform_dual.py (v7 - Dual-Channel Version) —— v6 Anti-Avalanche ARQ + Dual-Channel Waveform Display

Channel Definitions:
  ch1 = Voltage
  ch2 = Current

Data Format (Dual-Channel):
  1028 bytes per packet = 4-byte sequence number + 256 samples × 4 bytes
  4 bytes per sample: Lower 16 bits = ch1 (Voltage), Upper 16 bits = ch2 (Current)
  Both are 14-bit left-aligned (<<2) unsigned, full scale 65532, step size 4

ARQ part is identical to v6 (token bucket rate limiting + relaxed retries + print rate limiting), unchanged.

Usage:
    python pc_waveform_dual.py            # Statistics + Dual-channel waveform
    python pc_waveform_dual.py --no-plot  # Statistics only
    python pc_waveform_dual.py --overlay  # Overlay both channels on the same plot
"""

import argparse
import queue
import socket
import struct
import threading
import time

import numpy as np

BOARD_IP = "192.168.1.100"
BOARD_PORT = 5001
PORT = 5001

# ---- [Dual-Channel] Packet Format ----
PKT_SAMPLES = 256                       # 512 -> 256 (4 bytes per sample to fit MTU)
PKT_BYTES = 4 + PKT_SAMPLES * 4         # = 1028, same packet size as single-channel mode

CH1_NAME = "Voltage (ch1)"
CH2_NAME = "Current (ch2)"

# Display Window Time = PLOT_WINDOW_PKTS × 256 ÷ 1MSPS
#   64 packets → 16384 samples → 16.4ms, exactly one sawtooth period
PLOT_WINDOW_PKTS = 64
PLOT_DECIM = 1
STATS_INTERVAL = 1.0
PLOT_INTERVAL = 0.2
RCVBUF_BYTES = 32 * 1024 * 1024

NACK_RETRY_S = 0.5            # Retry interval
NACK_MAX_TRIES = 3            # Max retries
NACK_RATE_LIMIT = 30          # Max NACKs per second (Token Bucket)
EVENT_PRINT_LIMIT = 5         # Max event log prints per second


class Receiver(threading.Thread):
    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.expected = None
        self.n_pkts = 0
        self.n_gaps = 0
        self.n_recovered = 0
        self.n_permanent = 0
        self.n_nack_dropped = 0       # Number of gaps dropped due to rate limiting
        self.n_bytes = 0
        self.pending = {}
        self.plock = threading.Lock()
        self.wlock = threading.Lock()
        self.window1 = []             # [Dual-Channel] ch1 Voltage
        self.window2 = []             # [Dual-Channel] ch2 Current
        self.events = queue.Queue(maxsize=200)
        self._decim = 0
        # NACK Token Bucket
        self._tokens = float(NACK_RATE_LIMIT)
        self._token_t = time.time()
        # Print Rate Limiting
        self._evt_win_t = time.time()
        self._evt_win_n = 0

    # ---------- Token Bucket ----------
    def _take_token(self):
        now = time.time()
        self._tokens = min(NACK_RATE_LIMIT,
                           self._tokens + (now - self._token_t) * NACK_RATE_LIMIT)
        self._token_t = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    # ---------- Rate-Limited Logging ----------
    def _event(self, msg):
        now = time.time()
        if now - self._evt_win_t >= 1.0:
            self._evt_win_t = now
            self._evt_win_n = 0
        if self._evt_win_n < EVENT_PRINT_LIMIT:
            self._evt_win_n += 1
            try:
                self.events.put_nowait(msg)
            except queue.Full:
                pass

    def send_nack(self, seq):
        try:
            self.sock.sendto(struct.pack("<I", seq & 0xFFFFFFFF),
                             (BOARD_IP, BOARD_PORT))
        except OSError:
            pass

    def _ingest_plot(self, data):
        self._decim += 1
        if self._decim < PLOT_DECIM:
            return
        self._decim = 0
        # [Dual-Channel] Each sample is one u32: Lower 16 bits = ch1 (Voltage), Upper 16 bits = ch2 (Current)
        d32 = np.frombuffer(data, dtype="<u4", offset=4)
        ch1 = (d32 & 0xFFFF).astype(np.uint16)
        ch2 = (d32 >> 16).astype(np.uint16)
        with self.wlock:
            self.window1.append(ch1)
            self.window2.append(ch2)
            if len(self.window1) > PLOT_WINDOW_PKTS:
                self.window1.pop(0)
                self.window2.pop(0)

    def handle(self, data):
        if len(data) != PKT_BYTES:
            return
        (seq,) = struct.unpack_from("<I", data, 0)
        self.n_bytes += len(data)

        if self.expected is None:
            self.expected = (seq + 1) & 0xFFFFFFFF
            self.n_pkts += 1
            self._ingest_plot(data)
            return

        diff = (seq - self.expected) & 0xFFFFFFFF

        if diff == 0:
            self.n_pkts += 1
            self.expected = (seq + 1) & 0xFFFFFFFF
            self._ingest_plot(data)
        elif diff < 1000000:
            now = time.time()
            with self.plock:
                for miss in range(self.expected, self.expected + diff):
                    m = miss & 0xFFFFFFFF
                    if m in self.pending:
                        continue
                    self.n_gaps += 1
                    if self._take_token():          # Request retry only if token is available
                        self.pending[m] = [now, 1, now]
                        self.send_nack(m)
                        self._event(f">> GAP seq {m} -> NACK")
                    else:                            # Drop due to rate limiting
                        self.n_permanent += 1
                        self.n_nack_dropped += 1
            self.n_pkts += 1
            self.expected = (seq + 1) & 0xFFFFFFFF
            self._ingest_plot(data)
        else:
            with self.plock:
                if seq in self.pending:
                    del self.pending[seq]
                    self.n_recovered += 1
                    self.n_pkts += 1
                    self._event(f"<< RECOVERED seq {seq}")

    def run(self):
        sock = self.sock
        sock.settimeout(0.05)
        while True:
            try:
                data, _ = sock.recvfrom(2048)
                self.handle(data)
            except socket.timeout:
                pass
            sock.setblocking(False)
            try:
                while True:
                    data, _ = sock.recvfrom(2048)
                    self.handle(data)
            except (BlockingIOError, OSError):
                pass
            finally:
                sock.settimeout(0.05)

            now = time.time()
            with self.plock:
                for m in list(self.pending.keys()):
                    ent = self.pending.get(m)
                    if ent is None:
                        continue
                    first, tries, last = ent
                    if now - last >= NACK_RETRY_S:
                        if tries >= NACK_MAX_TRIES:
                            del self.pending[m]
                            self.n_permanent += 1
                            self._event(f"xx GIVE UP seq {m}")
                        elif self._take_token():     # Retries also consume tokens
                            ent[1] += 1
                            ent[2] = now
                            self.send_nack(m)
                        else:
                            ent[2] = now             # Deferred to next round if out of tokens


def raise_priority():
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)
        print("Process priority: HIGH")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--overlay", action="store_true",
                    help="Overlay both channels on a single plot for phase relation analysis")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    raise_priority()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
    sock.bind(("0.0.0.0", args.port))
    print(f"Listening on UDP :{args.port} (dual-channel, pkt={PKT_BYTES}B, "
          f"{PKT_SAMPLES} pairs/pkt, nack_limit={NACK_RATE_LIMIT}/s)")
    print(f"   ch1 = {CH1_NAME}   ch2 = {CH2_NAME}")

    rx = Receiver(sock)
    rx.start()

    plot = not args.no_plot
    if plot:
        import matplotlib.pyplot as plt
        plt.ion()
        xmax = PLOT_WINDOW_PKTS * PKT_SAMPLES

        if args.overlay:
            # ---- Overlay Mode: Both channels on one plot to observe phase shift ----
            fig, ax1 = plt.subplots(figsize=(11, 4.5))
            (l1,) = ax1.plot([], [], lw=1, color="tab:blue", label=CH1_NAME)
            (l2,) = ax1.plot([], [], lw=1, color="tab:orange", label=CH2_NAME)
            ax1.set_ylim(0, 65535)
            ax1.set_xlim(0, xmax)
            ax1.set_xlabel("sample")
            ax1.set_ylabel("code (14bit << 2)")
            ax1.grid(True, alpha=0.3)
            ax1.legend(loc="upper right")
            axes = [ax1]
        else:
            # ---- Subplot Mode (Default): Top Voltage, Bottom Current ----
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5),
                                           sharex=True)
            (l1,) = ax1.plot([], [], lw=1, color="tab:blue")
            (l2,) = ax2.plot([], [], lw=1, color="tab:orange")
            for ax, name in ((ax1, CH1_NAME), (ax2, CH2_NAME)):
                ax.set_ylim(32000, 33500)
                ax.set_xlim(0, xmax)
                ax.set_ylabel("code")
                ax.grid(True, alpha=0.3)
                ax.set_title(name, fontsize=10, loc="left")
            ax2.set_xlabel("sample")
            fig.tight_layout()
            axes = [ax1, ax2]

    last_bytes = 0
    t_start = time.time()
    t_stats = time.time()
    t_plot = time.time()

    try:
        while True:
            time.sleep(0.02)

            try:
                while True:
                    print("  " + rx.events.get_nowait())
            except queue.Empty:
                pass

            now = time.time()

            if now - t_stats >= STATS_INTERVAL:
                cur = rx.n_bytes
                mbps = (cur - last_bytes) * 8 / (now - t_stats) / 1e6
                last_bytes = cur
                print(f"pkts={rx.n_pkts:>9}  gaps={rx.n_gaps:>5}  "
                      f"rec={rx.n_recovered:>5}  lost={rx.n_permanent}"
                      f"(rl_drop={rx.n_nack_dropped})  rate={mbps:7.2f} Mbps")
                t_stats = now

            if plot and now - t_plot >= PLOT_INTERVAL:
                with rx.wlock:
                    w1 = list(rx.window1)
                    w2 = list(rx.window2)
                if w1:
                    y1 = np.concatenate(w1)
                    y2 = np.concatenate(w2)
                    x = np.arange(len(y1))
                    l1.set_data(x, y1)
                    l2.set_data(x, y2)
                    title = (f"pkts={rx.n_pkts}  gaps={rx.n_gaps}  "
                             f"rec={rx.n_recovered}  lost={rx.n_permanent}")
                    fig.suptitle(title, fontsize=10)
                    fig.canvas.draw_idle()
                    fig.canvas.flush_events()
                t_plot = now

    except KeyboardInterrupt:
        total = rx.n_pkts + rx.n_permanent
        pct = rx.n_permanent / max(total, 1) * 100
        print(f"\n===== Summary =====\n"
              f"received       : {rx.n_pkts}\n"
              f"sample pairs   : {rx.n_pkts * PKT_SAMPLES}\n"
              f"gaps detected  : {rx.n_gaps}\n"
              f"recovered      : {rx.n_recovered}\n"
              f"PERMANENT LOST : {rx.n_permanent}  ({pct:.4f}%)\n"
              f"  of which rate-limit dropped: {rx.n_nack_dropped}\n"
              f"duration       : {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()

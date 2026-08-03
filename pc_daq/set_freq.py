#!/usr/bin/env python3
# Usage: python set_freq.py 2000000    (unit: Hz)
import socket, struct, sys

if len(sys.argv) != 2:
    print("usage: python set_freq.py <freq_hz>")
    sys.exit(1)

freq = int(sys.argv[1])
pkt = struct.pack("<III", 0x44445301, freq, 0)   # Magic number + frequency + reserved
socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
    pkt, ("192.168.1.100", 5001))
print(f"sent: set DDS to {freq} Hz")

import asyncio, struct, socket, sys, time, threading, csv, os, tempfile
import sys

# Set calibration to device
# Get parameters from script cli

TARGET_PORT = 10578
LOCAL_PORT = 10578
TIMEOUT_SEC = 0.5

# CRC-16/CCITT checksum
def crc16_ccitt(data: bytes, poly: int=0x1021, crc: int=0xFFFF) -> int:
    for b in data:
        crc ^= b<<8
        for _ in range(8):
            crc = ((crc<<1)^poly)&0xFFFF if crc&0x8000 else (crc<<1)&0xFFFF
    return crc

def set_calibration(ip_address: str, channels: int, info: list[tuple[float, float]], save: bool = True):
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", LOCAL_PORT))   # Bind to local port for receiving
    sock.settimeout(TIMEOUT_SEC)

    pkt = struct.pack('<IHH', 16, 0x00AC if save else 0, channels)
    for unit, offset, gain in info:
        pkt += struct.pack('<3sxff', unit.encode('utf-8'), offset, gain)
    crc = crc16_ccitt(pkt)
    pkt += struct.pack('<H', crc)

    sock.sendto(pkt, (ip_address, TARGET_PORT))

    # Wait for response
    try:
        data, addr = sock.recvfrom(1024)   # buffer size 1 KB
        print(f"Received {len(data)} bytes from {addr}: {data}")
    except socket.timeout:
        print("No response received (timeout).")

    sock.close()


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python set_calibration.py <IP_ADDRESS> <CHANNELS> [<UNIT0> <OFFSET0> <GAIN0> ... <UNITn> <OFFSETn> <GAINn>] [<SAVE>]")
        sys.exit(1)
    ip_address = sys.argv[1]
    channels = int(sys.argv[2])
    info = channels * [("ADC", 0.0, 1.0)]
    for i in range(channels):
        unit = sys.argv[3 + i*3]
        offset = float(sys.argv[4 + i*3])
        gain = float(sys.argv[5 + i*3])
        info[i] = (unit, offset, gain)
    if len(sys.argv) > 3 + channels*3:
        save = not (sys.argv[3 + channels*3].lower() in ['false', '0', 'no'])
    set_calibration(ip_address, channels, info, save)
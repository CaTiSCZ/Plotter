import asyncio, struct, socket, sys, time, threading, csv, os, tempfile
import sys

# Assign ID to device
# Get ID as parameter from script cli
# Find 192.168.137.100 device and assign ID if any

TARGET_IP = "192.168.137.100"
TARGET_PORT = 10578
LOCAL_PORT = 10578
TIMEOUT_SEC = 0.5

def _assign_id(new_id: int):
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", LOCAL_PORT))   # Bind to local port for receiving
    sock.settimeout(TIMEOUT_SEC)

    # Prepare message to send
    payload = struct.pack('<BB', new_id, 0xAC)
    pkt = struct.pack('<I', 13) + payload

    print(f"Set {TARGET_IP} for ID {new_id}")
    sock.sendto(pkt, (TARGET_IP, TARGET_PORT))

    # Wait for response
    try:
        data, addr = sock.recvfrom(1024)   # buffer size 1 KB
        print(f"Received {len(data)} bytes from {addr}: {data}")
    except socket.timeout:
        print("No response received (timeout).")
    
    threading.Event().wait(2)

    # Reset device
    payload = struct.pack('<B', 0xFE)
    pkt = struct.pack('<I', 14) + payload
    print(f"Restart device")
    sock.sendto(pkt, (TARGET_IP, TARGET_PORT))

    sock.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python assign_id.py <ID>")
        sys.exit(1)
    id = int(sys.argv[1])
    _assign_id(id)
import socket
import struct
import matplotlib.pyplot as plt
import numpy as np

def crc16_ibm(data: bytes, poly=0xA001):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly if (crc & 1) else crc >> 1
    return crc & 0xFFFF

UDP_IP = "127.0.0.1"
UDP_PORT_SEND = 9999
UDP_PORT_RECV = 9998
SAMPLES_PER_SIGNAL = 200
SIGNAL_TYPE = np.int16
MAX_ATTEMPTS = 3
RECV_TIMEOUT = 1.0
RECEIVER_IP = UDP_IP
RECEIVER_PORT = UDP_PORT_RECV

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT_RECV))
sock.settimeout(RECV_TIMEOUT)

def send_command(command_type, num_packets=0):
    cmd = struct.pack('<IQ', command_type, num_packets)
    sock.sendto(cmd, (UDP_IP, UDP_PORT_SEND))
    for _ in range(MAX_ATTEMPTS):
        try:
            return sock.recvfrom(4096)[0]
        except socket.timeout:
            continue
    return None

def verify_crc(packet):
    if struct.unpack('<H', packet[:2])[0] == 0:
        return packet
    received_crc = struct.unpack('<H', packet[-2:])[0]
    calc_crc = crc16_ibm(packet[:-2])
    return packet[:-2] if received_crc == calc_crc else None

def send_register(ip_str, port):
    ip = socket.inet_aton(ip_str)
    cmd = struct.pack('<I4sH', 2, ip, port)
    sock.sendto(cmd, (UDP_IP, UDP_PORT_SEND))
    try:
        resp, _ = sock.recvfrom(1024)
        if len(resp) >= 15:
            _, _, _, ip_b, p, idx = struct.unpack('<HHI4sHB', resp[:15])
            print(f"[OK] Registrován {socket.inet_ntoa(ip_b)}:{p} (#{idx})")
            return True
    except socket.timeout:
        pass
    print("[ERR] Registrace selhala")
    return False

def send_get_receivers():
    sock.sendto(struct.pack('<I', 4), (UDP_IP, UDP_PORT_SEND))
    try:
        packet, _ = sock.recvfrom(1024)
        n = (len(packet) - 8) // 6
        print(f"[INFO] Počet přijímačů: {n}")
    except socket.timeout:
        print("[ERR] Nelze získat seznam přijímačů.")

print("[0] Ping")
packet = send_command(0)
if not packet:
    print("[ERR] Žádná odpověď na Ping.")
else:
    print(f"[INFO] Ping OK")

print("[1] Get ID")
packet = send_command(1)
if not packet:
    print("[ERR] Žádná odpověď na Get ID.")
    exit()
data = verify_crc(packet)
if not data:
    print("[ERR] CRC chyba v ID paketu.")
    exit()

typ = struct.unpack('<H', data[:2])[0]
if typ != 1:
    print(f"[ERR] Očekáván typ 1, přišel {typ}")
    exit()

header_fmt = '<HHHHBBIIHBB30sH'
values = struct.unpack(header_fmt, data[:struct.calcsize(header_fmt)])
channels_count = values[-1]
print(f"[INFO] ID OK – {channels_count} kanálů")

offset = struct.calcsize(header_fmt)
for i in range(channels_count):
    unit, offset_q, gain = struct.unpack('<4sff', data[offset:offset+12])
    print(f"  Kanál {i+1}: {unit.decode().strip()} q={offset_q:.2f} k={gain:.2f}")
    offset += 12

print("[4] Get receivers")
send_get_receivers()

print("[2] Register self")
send_register(RECEIVER_IP, RECEIVER_PORT)

print("[4] Get receivers")
send_get_receivers()

print("[5] Get signal")
packet = send_command(5, num_packets=1)
if not packet:
    print("[ERR] Signál nebyl přijat.")
    exit()
data = verify_crc(packet)
if not data:
    print("[ERR] CRC chyba v datovém paketu.")
    exit()

packet_type = struct.unpack('<H', data[:2])[0]
while packet_type == 0:
    packet, _ = sock.recvfrom(4096)
    data = verify_crc(packet)
    if not data:
        exit()
    packet_type = struct.unpack('<H', data[:2])[0]

_, packet_id = struct.unpack('<HH', data[:4])
print(f"[INFO] Signální paket #{packet_id}")

payload = data[4:]
arr = np.frombuffer(payload, dtype=SIGNAL_TYPE)
num_values = len(arr)

for n in range(1, 100):
    if n * (SAMPLES_PER_SIGNAL + 1) == num_values:
        num_signals = n
        break
else:
    print("[ERR] Chybný počet vzorků")
    exit()

print(f"[INFO] Signálů: {num_signals}")

signals = []
for i in range(num_signals):
    s = i * SAMPLES_PER_SIGNAL
    e = (i + 1) * SAMPLES_PER_SIGNAL
    signals.append(arr[s:e])

errors = arr[num_signals * SAMPLES_PER_SIGNAL:]
print("Chyby:")
for i, err in enumerate(errors):
    print(f"  Signál {i+1}: {err}")

x = np.arange(SAMPLES_PER_SIGNAL)
plt.figure()
for i, sig in enumerate(signals):
    plt.plot(x, sig, label=f"Signál {i+1}")
plt.legend()
plt.title("Přijaté signály")
plt.xlabel("Vzorek")
plt.ylabel("Hodnota")
plt.grid()
plt.show()

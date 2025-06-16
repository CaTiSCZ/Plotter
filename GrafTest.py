import socket
import struct
import numpy as np
from PyQt5.QtWidgets import QApplication
import pyqtgraph as pg

# ---------------------- Parametry ----------------------
UDP_IP = "127.0.0.1"
UDP_PORT_SEND = 9999  # generátor
UDP_PORT_RECV = 9998  # tento klient
NUM_SAMPLES = 10      # počet vzorků (požadavek v CMD 5)
RECV_TIMEOUT = 2.0
SAMPLES_PER_PACKET = 200
SIGNAL_TYPE = np.int16

# ---------------------- CRC CCITT ----------------------
def crc16_ccitt(data: bytes, poly=0x1021, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc

def verify_crc(pkt):
    if len(pkt) < 2:
        return None
    data = pkt[:-2]
    received_crc = struct.unpack('<H', pkt[-2:])[0]
    if crc16_ccitt(data) != received_crc:
        return None
    return data

# ---------------------- Socket Setup ----------------------
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT_RECV))
sock.settimeout(RECV_TIMEOUT)

# ---------------------- Odesílání příkazů ----------------------
def send_command(cmd: int, data: bytes = b''):
    pkt = struct.pack('<I', cmd) + data
    sock.sendto(pkt, (UDP_IP, UDP_PORT_SEND))
    print(f"[SEND] CMD {cmd}, data={data.hex()}")

# ---------------------- Ping ----------------------
send_command(0)
try:
    ack, _ = sock.recvfrom(1024)
    print("[OK] Ping")
except socket.timeout:
    print("[ERR] Ping selhal")
    exit()

# ---------------------- Get ID ----------------------
ID_HEADER_STRUCT = struct.Struct('<HHHHBBIIHBB30sH')

def parse_id_packet(data):
    if len(data) < ID_HEADER_STRUCT.size:
        raise ValueError("Paket příliš krátký")
    unpacked = ID_HEADER_STRUCT.unpack(data[:ID_HEADER_STRUCT.size])
    return {
        'packet_type': unpacked[0],
        'state': unpacked[1],
        'hw_id': unpacked[2],
        'hw_ver_major': unpacked[3],
        'hw_ver_minor': unpacked[4],
        'hw_mcu_serial': unpacked[6],
        'hw_adc_serial': unpacked[7],
        'fw_id': unpacked[8],
        'fw_ver_major': unpacked[9],
        'fw_ver_minor': unpacked[10],
        'build_time': unpacked[11].decode('ascii').strip('\x00'),
        'channels_count': unpacked[12]
    }

send_command(1)
try:
    id_pkt, _ = sock.recvfrom(1024)
    data = verify_crc(id_pkt)
    if not data:
        raise ValueError("Chybný CRC nebo prázdný paket")
    
    parsed = parse_id_packet(data)
    if parsed['packet_type'] != 1:
        raise ValueError("Nesprávný typ ID paketu")

    ch = parsed['channels_count']
    print(f"[INFO] Počet kanálů: {ch}")
    print(f"[INFO] Build time: {parsed['build_time']}")
    print(f"[INFO] FW v{parsed['fw_ver_major']}.{parsed['fw_ver_minor']} (ID: {hex(parsed['fw_id'])})")
    if ch == 0:
        raise ValueError("Počet kanálů je 0, nelze pokračovat")

except Exception as e:
    print("[ERR] Get ID selhal:", e)
    exit()


# ---------------------- Register receiver ----------------------
ip_bytes = socket.inet_aton(UDP_IP)
port_bytes = struct.pack('<H', UDP_PORT_RECV)
send_command(2, ip_bytes + port_bytes)

try:
    ack, _ = sock.recvfrom(1024)
    print("[INFO] Registrován jako příjemce")
except socket.timeout:
    print("[WARN] Nepřišlo ACK na registraci")

# ---------------------- Get receivers ----------------------
send_command(4)
try:
    ack, _ = sock.recvfrom(1024)
    data = verify_crc(ack)
    if not data:
        raise ValueError("Chybný CRC u ACK receiverů")
    receivers = []
    offset = 8
    while offset + 6 <= len(data):
        ip = socket.inet_ntoa(data[offset:offset + 4])
        port = struct.unpack('<H', data[offset + 4:offset + 6])[0]
        receivers.append((ip, port))
        offset += 6
    print(f"[INFO] Seznam receiverů: {receivers}")
except Exception as e:
    print("[WARN] Nelze získat seznam receiverů:", e)

# ---------------------- Start sampling ----------------------
num_samples_bytes = struct.pack('<Q', NUM_SAMPLES)
send_command(5, num_samples_bytes)

# ---------------------- Příjem dat ----------------------
datas = []
while len(datas) < NUM_SAMPLES:
    try:
        pkt, _ = sock.recvfrom(4096)
        print(f"[RECV] Paket délky {len(pkt)} přijat")   # Debug výpis délky paketu
        d = verify_crc(pkt)
        if not d:
            print("[WARN] CRC neprošel, paket zahazuji")  # Pokud CRC nesedí, upozorní
            continue
        print(f"[DEBUG] Paket po CRC (prvních 10 bajtů): {d[:10].hex()}")  # Přidaný výpis dat paketu po CRC
        pkt_type = struct.unpack('<H', d[:2])[0]
        if pkt_type != 2:
            print(f"[WARN] Neznámý typ paketu: {pkt_type}, ignoruji")  # Ignorujeme jiné typy paketů
            continue
        datas.append(d)
    except socket.timeout:
        print("[WARN] Timeout na příjem paketu")  # Timeout při čekání na paket
        break

if len(datas) == 0:
    print("[ERR] Nepřišla žádná data")
    exit()

# ---------------------- Zpracování ----------------------
# 1 paket = 4B hlavička + ch*200*2B signál + ch*1B chyby + padding
signals = [[] for _ in range(ch)]
errors = [[] for _ in range(ch)]

for pkt in datas:
    header, pkt_num = struct.unpack('<HH', pkt[:4])
    print(f"Zpracovávám paket číslo {pkt_num}, hlavička: {header}")  # Debug výpis čísla paketu a hlavičky
    offset = 4
    for i in range(ch):
        sig = struct.unpack('<' + 'h'*SAMPLES_PER_PACKET, pkt[offset:offset+400])
        print(f"Kanál {i} - první 5 vzorků: {sig[:5]}")  # Výpis prvních 5 vzorků kanálu
        signals[i].extend(sig)
        offset += 400
    for i in range(ch):
        errors[i].append(pkt[offset])
        offset += 1

print(f"[DEBUG] Počet vzorků na kanál: {[len(s) for s in signals]}")  # Výpis délky dat v každém kanálu


# ---------------------- Vykreslení ----------------------
app = QApplication([])
win = pg.GraphicsLayoutWidget(show=True, title="Signály")
plot = win.addPlot(title="Signály ze všech kanálů")
plot.showGrid(x=True, y=True)
curves = [plot.plot(pen=pg.intColor(i, hues=ch)) for i in range(ch)]

min_len = min(len(sig) for sig in signals)
for i in range(ch):
    curves[i].setData(signals[i][:min_len])

print("Chybové hodnoty na kanál:")
for i, err_list in enumerate(errors):
    print(f"  Kanál {i}: {err_list}")

app.exec_()

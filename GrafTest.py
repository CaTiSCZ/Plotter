import socket
import struct
import matplotlib.pyplot as plt
import numpy as np

# --- CRC-16 (Modbus / CRC-16-IBM) ---
def crc16_ibm(data: bytes, poly=0xA001):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if (crc & 1):
                crc = (crc >> 1) ^ poly
            else:
                crc >>= 1
    return crc & 0xFFFF

# --- Parametry ---
UDP_IP = "127.0.0.1"
UDP_PORT_SEND = 9999
UDP_PORT_RECV = 9998
SAMPLES_PER_SIGNAL = 200
SIGNAL_TYPE = np.int16
MAX_ATTEMPTS = 3
RECV_TIMEOUT = 1.0

# --- Socket ---
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT_RECV))
sock.settimeout(RECV_TIMEOUT)

def send_command(command_type, num_packets=0):
    command_packet = struct.pack('<IQ', command_type, num_packets)
    sock.sendto(command_packet, (UDP_IP, UDP_PORT_SEND))

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"[{attempt}/{MAX_ATTEMPTS}] Čekám na odpověď pro příkaz {command_type}...")
            packet, addr = sock.recvfrom(4096)
            print(f"Přijato {len(packet)} bajtů od {addr}")
            return packet
        except socket.timeout:
            print("⚠️ Timeout.")
        except Exception as e:
            print(f"⚠️ Chyba: {e}")
    return None

def verify_crc(packet):
    packet_type = struct.unpack('<H', packet[:2])[0]

    # Přeskočit CRC kontrolu pro ACK paket (typ 0)
    if packet_type == 0:
        print("ACK paket – CRC se neprovádí.")
        return packet

    # Pro ostatní pakety proveď kontrolu CRC
    received_crc = struct.unpack('<H', packet[-2:])[0]
    crc_input = packet[:-2]
    calculated_crc = crc16_ibm(crc_input)

    if received_crc != calculated_crc:
        print(f"CRC ERROR! Očekáváno {hex(calculated_crc)}, přijato {hex(received_crc)}")
        return None

    print("CRC OK")
    return crc_input

# --- 1. Get ID ---
print("\n--- Odesílám příkaz Get ID ---")
packet = send_command(1)
if packet is None:
    print("Chyba: Nepodařilo se získat identifikaci.")
    exit()

data_part = verify_crc(packet)
if data_part is None:
    exit()

# --- Zpracuj ID paket ---
packet_type = struct.unpack('<H', data_part[:2])[0]
if packet_type != 1:
    print(f"Očekáván paket typu 1 (ID), ale přišel {packet_type}")
    exit()

print("Zpracovávám identifikační paket...")
unpack_fmt = '<HHHHBBIIHBB30sH'
header_len = struct.calcsize(unpack_fmt)
values = struct.unpack(unpack_fmt, data_part[:header_len])

fields = [
    "Packet type", "Error/state", "HW ID", "HW ver major", "HW ver minor", "pad",
    "HW MCU serial", "HW ADC serial", "FW ID", "FW ver major", "FW ver minor",
    "Build time", "Channels count"
]

for name, val in zip(fields, values):
    if isinstance(val, bytes):
        val = val.decode('ascii').strip('\x00')
    print(f"{name}: {val}")

channels_count = values[-1]
offset = header_len
for i in range(channels_count):
    chan = struct.unpack('<4sff', data_part[offset:offset + 12])
    unit = chan[0].decode('ascii').strip('\x00')
    offset_q, offset_k = chan[1], chan[2]
    print(f"Kanál {i}: jednotka = '{unit}', offset = {offset_q}, gain = {offset_k}")
    offset += 12

# --- 2. Get signal packet ---
print("\n--- Odesílám příkaz Get Signal ---")
packet = send_command(5, num_packets=1)
if packet is None:
    print("Chyba: Nepodařilo se získat datový paket.")
    exit()

data_part = verify_crc(packet)
if data_part is None:
    exit()

packet_type = struct.unpack('<H', data_part[:2])[0]
if packet_type == 0:
    print("ACK paket – čekám na další paket...")
    # tady je potřeba přijmout další paket, protože ACK není datový paket
    # můžeš buď zkusit recvfrom znovu nebo celý blok dát do smyčky
    # příklad smyčky:
    while packet_type == 0:
        packet, addr = sock.recvfrom(4096)
        data_part = verify_crc(packet)
        if data_part is None:
            exit()
        packet_type = struct.unpack('<H', data_part[:2])[0]

# nyní pokračuj se zpracováním datového paketu typu 2
packet_type, packet_id = struct.unpack('<HH', data_part[:4])
print(f"Typ paketu: {packet_type}, ID: {packet_id}")

payload = data_part[4:]
data = np.frombuffer(payload, dtype=SIGNAL_TYPE)
num_values = len(data)

for possible_n in range(1, 100):
    if possible_n * (SAMPLES_PER_SIGNAL + 1) == num_values:
        num_signals = possible_n
        break
else:
    print("Neplatný počet vzorků!")
    exit()

print(f"Počet signálů: {num_signals}")

signals = []
for i in range(num_signals):
    start = i * SAMPLES_PER_SIGNAL
    end = (i + 1) * SAMPLES_PER_SIGNAL
    signals.append(data[start:end])

error_start = num_signals * SAMPLES_PER_SIGNAL
error_end = error_start + num_signals
error_counts = data[error_start:error_end]

print("Chybové hodnoty pro jednotlivé signály:")
for i, err in enumerate(error_counts):
    print(f"  Signál {i+1}: {err} chyb")

# --- Vykreslení ---
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

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
UDP_PORT = 9999
SAMPLES_PER_SIGNAL = 200
SIGNAL_TYPE = np.int16

# --- Přijmi jeden UDP paket ---
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
print(f"Čekám na 1 paket na {UDP_IP}:{UDP_PORT}...")
packet, addr = sock.recvfrom(4096)
print(f"Přijato {len(packet)} bajtů od {addr}")

# --- CRC kontrola ---
if len(packet) < 6:
    print("Paket je příliš krátký.")
    exit()

data_part = packet[:-2]
received_crc = struct.unpack('<H', packet[-2:])[0]
calculated_crc = crc16_ibm(data_part)

if received_crc != calculated_crc:
    print(f"CRC ERROR! Očekáváno {hex(calculated_crc)}, přijato {hex(received_crc)}")
    exit()
else:
    print("CRC OK")

# --- Parsování hlavičky ---
packet_type, packet_id = struct.unpack('<HH', data_part[:4])
print(f"Typ paketu: {packet_type}, ID: {packet_id}")

# --- Rozparsuj data ---
payload = data_part[4:]  # bez hlavičky
#num_values = len(payload) // 2  # počet int16
#max_possible_signals = num_values // (SAMPLES_PER_SIGNAL + 1)  # minimální možné maximum (kdyby byl 1 error za signál)

data = np.frombuffer(payload, dtype=SIGNAL_TYPE)
num_values = len(data)

# --- Detekuj počet signálů ---
# Hledáme takové N, že N * (SAMPLES_PER_SIGNAL + 1) == num_values
for possible_n in range(1, 100):  # přiměřený rozsah pro testy
    if possible_n * (SAMPLES_PER_SIGNAL + 1) == num_values:
        num_signals = possible_n
        break
else:
    print("Neplatný počet vzorků!")
    exit()

num_signals = num_values // SAMPLES_PER_SIGNAL
print(f"Počet signálů: {num_signals}")


if num_signals is None:
    print("Neplatná struktura paketu – nelze rozdělit na signály a chyby.")
    exit()
# --- Rozdělení signálů ---
signals = []
for i in range(num_signals):
    start = i * SAMPLES_PER_SIGNAL
    end = (i + 1) * SAMPLES_PER_SIGNAL
    signals.append(data[start:end])

# --- Chybové hodnoty ---
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

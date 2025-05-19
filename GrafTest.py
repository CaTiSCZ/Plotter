import socket
import struct
import matplotlib.pyplot as plt
import numpy as np
import time

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
RECV_TIMEOUT = 3.0  # v sekundách

# --- Vytvoř socket a odešli příkazový paket ---
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT_RECV))
sock.settimeout(RECV_TIMEOUT)

def send_command_and_receive():
    for attempt in range (1, MAX_ATTEMPTS + 1):
        try:
            print(f"\n[Pokus {attempt}/{MAX_ATTEMPTS}] Odesílám příkaz...")

            # Odeslání příkazového paketu (command_type=5, num_packets=1)
            command_type = 5
            num_packets = 1
            command_packet = struct.pack('<IQ', command_type, num_packets)
            sock.sendto(command_packet, (UDP_IP, UDP_PORT_SEND))


            # --- Přijmi potvrzení ---
            ack_packet, _ = sock.recvfrom(1024)
            ack_type, ack_error, ack_cmd, ack_count = struct.unpack('<HHIQ', ack_packet)
            print(f"Přijato potvrzení: typ={ack_type}, error={ack_error}, command={ack_cmd}, count={ack_count}")


            # --- Přijmi datový paket ---
            print(f"Čekám na datový paket na {UDP_IP}:{UDP_PORT_RECV}...")
            packet, addr = sock.recvfrom(4096)
            print(f"Přijato {len(packet)} bajtů od {addr}")
            return packet  # úspěch
        except socket.timeout:
            print("⚠️  Timeout při čekání na odpověď.")
        except Exception as e:
            print(f"⚠️  Chyba: {e}")

    return None  # po všech pokusech

# --- Hlavní smyčka ---
while True:
    packet = send_command_and_receive()
    if packet is not None:
        break

    # Po 3 neúspěšných pokusech se ptáme uživatele
    user_input = input("\n❌ Nepodařilo se odeslat příkaz. Chceš to zkusit znovu? [a/n]: ").strip().lower()
    if user_input != 'a':
        print("Ukončuji program.")
        exit()

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
data = np.frombuffer(payload, dtype=SIGNAL_TYPE)
num_values = len(data)

# --- Detekuj počet signálů ---
for possible_n in range(1, 100):
    if possible_n * (SAMPLES_PER_SIGNAL + 1) == num_values:
        num_signals = possible_n
        break
else:
    print("Neplatný počet vzorků!")
    exit()

print(f"Počet signálů: {num_signals}")

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

import socket
import threading
import time
import struct
import numpy as np
import argparse

def crc16(data: bytes, poly=0xA001):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if (crc & 1):
                crc = (crc >> 1) ^ poly
            else:
                crc >>= 1
    return crc & 0xFFFF

class MultiSignalTestGenerator:
    def __init__(self, ip='127.0.0.1', port=9999, interval=0.001, num_signals=1):
        self.ip = ip
        self.port = port
        self.interval = interval
        self.num_signals = num_signals
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = False
        self.packet_id = 0

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False
        self.sock.close()

    def _run(self):
        length = 200
        int16_min = -32768
        int16_max = 32767
        base_signal_raw = np.arange(length, dtype=np.float32)
        base_signal_scaled = ((base_signal_raw / (length - 1)) * (int16_max - int16_min) + int16_min).astype(np.int16)

        while self.running:
            signals = []
            for i in range(self.num_signals):
                shift = (i * length) // self.num_signals
                signal = np.roll(base_signal_scaled + self.packet_id, shift)
                signals.append(signal.astype(np.int16))

            # Konverze všech signálů do bytového formátu
            signal_bytes = b''.join(s.tobytes() for s in signals)
            
            # Chybové hodnoty – zatím 0 pro každý signál
            error_counts = struct.pack('<' + 'H' * self.num_signals, *([0] * self.num_signals))

            # Hlavička: typ paketu (2), číslo paketu
            header = struct.pack('<HH', 2, self.packet_id % 65536)
            packet = header + signal_bytes + error_counts

            # Zarovnání na sudý počet bajtů
            if len(packet) % 2 != 0:
                packet += b'\x00'

            # Přidání CRC-16
            crc = crc16(packet)
            packet += struct.pack('<H', crc)

            # Odeslání UDP paketu
            self.sock.sendto(packet, (self.ip, self.port))
            break
            self.packet_id += 1
            time.sleep(self.interval)

# --- Spuštění jako program ---
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Testovací UDP generátor více signálů")
    parser.add_argument('--signals', type=int, default=1, help="Počet signálů v jednom packetu")
    args = parser.parse_args()

    gen = MultiSignalTestGenerator(num_signals=args.signals)
    gen.start()
    print(f"Generátor spuštěn: {args.signals} signál(ů), Ctrl+C pro ukončení.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nUkončuji...")
        gen.stop()

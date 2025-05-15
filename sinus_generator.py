"""
sinus_generator.py
spustím program, řeknu port na kterém má poslouchat - parametr při spuštění
poslouchám
přijde žádost o identifikaci
identifikuju se - přepočet z binárky na čísla (32 767 = 200 A), identifikátor hardware a firmware, počet kanálů
    buď U+I nebo U+U+I - řekni jak jsou data po sobě (počítám od jedničky)
    označení že jde o identifikační packet (0)

poslouchám, přijde žádost (UDP packet) o data 
začínám vysílat data

generuje signál sinusoidy a posílá protokolem UDP jako binárku v rozsahu int 16 (=32 767)
possílání dat na dotaz z plotteru
identifikařní packet s udaji o signálu zaslaný na požádání
typ generovaného signálu a podoba packetu: 
dva nebo 3 signály - sin (+sin posunutý) a triangel 
zadám počet kanálů a rozsahy (maximální hodnoty)
jeden packet obsahuje 200 hodnoto od každého signálu, prvně jeden signál, pak druhý
začátek paketu:
    - označení že jde o datový packet (1) číslo o velikosti 16b
    - pořadové číslo - pakety nemají čas, ale jsou očíslované, počítám od 0 (16b číslo)
    - informační číslo kolik vzorků se nepovedlo přečíst (bude tam 0 nebo zapínatelný generátor náhodných čísel) 8b
        pro každý kanál zvlášť
    - CRC - kontrola kopletnosti přenosu dat (jakoby kontrolní součet) 32b 
        celkový počet B %4 = 0, volné B před CRC
na paremetru můžeš nastavít - náhodně zahoď občas nějaký packet

"""
import numpy as np
import threading
import time
import socket
import struct

def crc16_ibm(data: bytes, poly=0x8005, init=0x0000) -> int:
    crc = init
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

class DualSignalGenerator:
    def __init__(self, dx=0.0001, interval=0.001, ip='127.0.0.1', port=9999):
        self.dx = dx
        self.interval = interval
        self.x = 0
        self.packet_number = 0
        self.running = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (ip, port)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join()
        self.sock.close()

    def _run(self):
        samples_per_packet = 200
        triangle_period = 1.0

        while self.running:
            voltages = []
            currents = []

            for _ in range(samples_per_packet):
                voltage = np.sin(self.x)
                triangle_phase = (self.x % triangle_period) / triangle_period
                current = 10 * (2 * abs(triangle_phase - 0.5))

                # škálování do rozsahu int16
                voltage_int = int(voltage * 32767)          # -1.0 až 1.0 → -32767 až 32767
                current_int = int((current / 10.0) * 32767)  # 0–10 A → 0–32767

                voltages.append(voltage_int)
                currents.append(current_int)
                self.x += self.dx

            # Header: 2 bajty typ paketu (2), 2 bajty číslo paketu
            packet_type = 2
            header = struct.pack('<HH', packet_type, self.packet_number)

            # Data: 200 int16 pro napětí, pak 200 int16 pro proud
            voltage_bytes = struct.pack(f'<{samples_per_packet}h', *voltages)
            current_bytes = struct.pack(f'<{samples_per_packet}h', *currents)

            payload = header + voltage_bytes + current_bytes

            # Zarovnat na sudý počet bajtů
            if len(payload) % 2 != 0:
                payload += b'\x00'

            # CRC-16
            crc = crc16_ibm(payload)
            crc_bytes = struct.pack('<H', crc)

            message = payload + crc_bytes

            #odeslání packetu
            self.sock.sendto(message, self.target)
            break
            self.packet_number = (self.packet_number + 1) % 65536
            time.sleep(self.interval)

# --- Spuštění ---
if __name__ == "__main__":
    gen = DualSignalGenerator(dx=0.0001, interval=0.001, ip='127.0.0.1', port=9999)
    gen.start()
    print("Generátor napětí a proudu spuštěn. Ukonči pomocí Ctrl+C.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nUkončuji generátor...")
    finally:
        gen.stop()
        print("Generátor ukončen.")


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
    def __init__(self, ip='127.0.0.1', port=9999, interval=0.001, num_signals = 1):
        self.ip = ip
        self.port = port
        self.interval = interval
        self.num_signals = num_signals
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.ip, self.port))
        self.sock.settimeout(1.0)  # ← timeout na recvfrom
        self.running = False
        self.packet_id = 0
        self.num_packets_to_send = 0  # 0 = continuous

    def start(self):
        self.running = True 
        self.listener_thread = threading.Thread(target=self._listen_for_command)
        self.listener_thread.start()

    def stop(self):
        self.running = False
        if hasattr(self, 'listener_thread'):
            self.listener_thread.join()
        self.sock.close()

    def _listen_for_command(self):
        print("Čekám na příkazový paket...")
        while self.running:
            try:
                cmd_data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue  # → umožní kontrolu self.running
            if len(cmd_data) == 12:
                command_type, num_packets = struct.unpack('<IQ', cmd_data)
                if command_type == 5:
                    print(f"Přijat příkaz: typ={command_type}, počet paketů={num_packets}")
                    self.num_packets_to_send = num_packets

                    # vynucení odpovědi na port 9998
                    response_addr = (addr[0], 9998)

                    # Odpověď (potvrzení příjmu)
                    response = struct.pack('<HHIQ', 0, 0, command_type, num_packets)
                    self.sock.sendto(response, response_addr)

                    # Spusť odesílání dat
                    self._send_data(addr)

                elif command_type == 1:
                    print("Přijat požadavek na identifikační paket.")
                    self._send_identification_packet(addr)

                else:
                    print(f"Neznámý příkaz typu {command_type}")

    
    def _send_identification_packet(self, addr):
        # TODO: Nahraď tyto hodnoty reálnými podle potřeby
        packet_type = 1
        state = 0
        hw_id = 0x1234
        hw_ver_major = 1
        hw_ver_minor = 0
        hw_mcu_serial = 0x11223344
        hw_adc_serial = 0x55667788
        fw_id = 0xABCD
        fw_ver_major = 2
        fw_ver_minor = 3
        build_time = b"2025-05-13T12:43:13\0".ljust(30, b'\x00')
        channels_count = self.num_signals
        units_and_gains = b''

        for i in range(channels_count):
            if i == 0:
                unit = b"mV\0".ljust(4, b'\x00')
                offset = 0.0
                gain = 1.0
            elif i == 1:
                unit = b"A\0".ljust(4, b'\x00')
                offset = 0.0
                gain = 1.0
            else:
                unit = b"?\0".ljust(4, b'\x00')
                offset = 0.0
                gain = 1.0
            units_and_gains += struct.pack('<4sff', unit, offset, gain)

        header = struct.pack(
            '<HHHHBBIIHBB30sH',
            packet_type,
            state,
            hw_id,
            hw_ver_major,
            hw_ver_minor,
            0,  # padding
            hw_mcu_serial,
            hw_adc_serial,
            fw_id,
            fw_ver_major,
            fw_ver_minor,
            build_time,
            channels_count
        )

        full_packet = header + units_and_gains
        crc = crc16(full_packet)
        full_packet += struct.pack('<H', crc)

        data_addr = (addr[0], 9998)
        self.sock.sendto(full_packet, data_addr)

        
    def _send_data(self, addr):
        base_signal = np.linspace(-32768, 32767, 200, dtype=np.int16)
        packets_sent = 0

        while self.running:
            signals = []
            for i in range(self.num_signals):
                shift = (i * 200) // self.num_signals
                signal = np.roll(base_signal + self.packet_id, shift)
                signals.append(signal.astype(np.int16))

            signal_bytes = b''.join(s.tobytes() for s in signals)

            # Chybové hodnoty – zatím 0 pro každý signál
            error_counts = struct.pack('<' + 'H' * self.num_signals, *([0] * self.num_signals))

            header = struct.pack('<HH', 2, self.packet_id % 65536)
            packet = header + signal_bytes + error_counts

            if len(packet) % 2 != 0:
                packet += b'\x00'

            crc = crc16(packet)
            packet += struct.pack('<H', crc)

            data_addr = (addr[0], 9998)  # Zajisti, že data jdou na správný port
            self.sock.sendto(packet, data_addr)
            self.packet_id += 1
            packets_sent += 1

            if self.num_packets_to_send != 0 and packets_sent >= self.num_packets_to_send:
                break

            time.sleep(self.interval)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Testovací UDP generátor více signálů")
    parser.add_argument('--signals', type=int, default=2, help="Počet signálů v jednom packetu")
    args = parser.parse_args()

    gen = MultiSignalTestGenerator(num_signals=args.signals)
    gen.start()
    print(f"Generátor připraven (signálů: {args.signals}). Čekám na příkaz.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nUkončuji...")
        gen.stop()

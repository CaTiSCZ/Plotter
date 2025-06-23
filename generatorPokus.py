import socket
import threading
import time
import struct
import numpy as np
import argparse
from collections import deque

def crc16_ccitt(data: bytes, poly=0x1021, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


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
        self.receivers = []  # ← seznam (IP, port)
        self.packets_sent = 0
        self.sampling = False
        self.sender_thread = None

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
                continue

            if len(cmd_data) < 4:
                continue  # Příliš krátký paket, ignoruj

            command_type = struct.unpack('<I', cmd_data[:4])[0]

            if command_type == 0:
                print("Přijat ping.")
                response = struct.pack('<HHI', 0, 0, 0)  # Packet type, error, CMD
                self.sock.sendto(response, addr)

            elif command_type == 1:
                self._send_identification_packet(addr)

            elif command_type == 2:
                self._register_receiver(cmd_data, addr)

            elif command_type == 3:
                self._remove_receiver(cmd_data, addr)

            elif command_type == 4:
                self._send_receivers_list(addr)

            elif command_type == 5 or command_type == 6:
                if len(cmd_data) < 12:
                    print(f"⚠️ CMD {command_type} má nedostatečnou délku.")
                    continue

                _, num_packets = struct.unpack('<IQ', cmd_data)
                print(f"Přijat příkaz: typ={command_type}, počet paketů={num_packets}")

                self.num_packets_to_send = num_packets
                self.packets_sent = 0  # reset počítadla
                self.sampling = True  # flag spuštění samplingu

                if command_type == 6:
                    print("[INFO] Sampling spuštěn na trigger (simulováno okamžitě).")
                    # TODO: zde může být logika pro trigger, nyní spustíme rovnou

                else:
                    print("[INFO] Sampling spuštěn.")

                # Odpověď: ACK + CMD + počet paketů
                response = struct.pack('<HHIQ', 0, 0, command_type, num_packets)
                self.sock.sendto(response, addr)

                # Spustit odesílání dat v samostatném vlákně, pokud ještě neběží
                if self.sender_thread is None or not self.sender_thread.is_alive():
                    self.sender_thread = threading.Thread(target=self._send_data_to_all_receivers, daemon=True)
                    self.sender_thread.start()

            elif command_type == 7:
                # Stop sampling
                self.sampling = False
                print(f"[INFO] Sampling zastaven, odesláno paketů: {self.packets_sent}")

                # Odpověď: ACK + CMD + počet odeslaných paketů
                response = struct.pack('<HHIQ', 0, 0, 7, self.packets_sent)
                self.sock.sendto(response, addr)

            elif command_type == 8:
                # Trigger ACK - potvrzení triggeru, nemusí nic dělat
                print("[INFO] Přijat Trigger ACK (CMD 8)")

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
        crc = crc16_ccitt(full_packet)
        full_packet += struct.pack('<H', crc)

        data_addr = addr
        self.sock.sendto(full_packet, data_addr)

    def _register_receiver(self, cmd_data, addr):
        ip_bytes = cmd_data[4:8]
        port = struct.unpack('<H', cmd_data[8:10])[0]

        if ip_bytes == b'\x00\x00\x00\x00':
            ip = addr[0]
        else:
            ip = '.'.join(str(b) for b in ip_bytes)

        if port == 0:
            port = addr[1]

        receiver = (ip, port)
        if receiver not in self.receivers:
            self.receivers.append(receiver)
            print(f"Registrován nový přijímač: {receiver}")
        else:
            print(f"Přijímač už existuje: {receiver}")

        index = self.receivers.index(receiver)

        # Odpověď: ACK + CMD + IP + Port + index
        response = struct.pack('<HHI4sHB',
            0,                # packet type (ACK)
            0,                # error/state
            2,                # CMD
            socket.inet_aton(ip),
            port,
            index             # pořadí v seznamu
        )
        self.sock.sendto(response, addr)

    def _remove_receiver(self, cmd_data, addr):
        ip_bytes = cmd_data[4:8]
        port = struct.unpack('<H', cmd_data[8:10])[0]
        ip = '.'.join(str(b) for b in ip_bytes)
        receiver = (ip, port)

        if receiver in self.receivers:
            self.receivers.remove(receiver)
            print(f"Odstraněn přijímač: {receiver}")
        else:
            print(f"Přijímač nenalezen: {receiver}")

        response = struct.pack('<HHI', 0, 0, 3)
        self.sock.sendto(response, addr)

    def _send_receivers_list(self, addr):
        response = struct.pack('<HHI', 0, 0, 4)

        for ip, port in self.receivers:
            ip_bytes = socket.inet_aton(ip)
            response += ip_bytes
            response += struct.pack('<H', port)

        self.sock.sendto(response, addr)

    def _send_data_to_all_receivers(self):
        
        period_length = 200000
        packet_size = 200
        base_signal = np.linspace(-32768, 32767, period_length, dtype=np.int16)
        
        print("[INFO] Zahájeno odesílání dat...")

        while self.running:
            if not self.sampling:
                time.sleep(0.1)
                continue

            if not self.receivers:
                print("[WAIT] Žádní příjemci. Čekám...")
                time.sleep(1)
                continue

            signals = []
            for i in range(self.num_signals):
                shift = (i * period_length) // self.num_signals
                start_index = (self.packet_id*packet_size + shift) % period_length
                if start_index + packet_size <= period_length:
                    chunk = base_signal[start_index:start_index + packet_size]
                else:
                    part1 = base_signal[start_index:]
                    part2 = base_signal[:packet_size - len(part1)]
                    chunk = np.concatenate((part1, part2))

                signals.append(chunk.astype(np.int16))

            signal_bytes = b''.join(s.tobytes() for s in signals)
            error_counts = struct.pack('<' + 'B' * self.num_signals, *([0] * self.num_signals))
            header = struct.pack('<HH', 2, self.packet_id % 65536)
            packet = header + signal_bytes + error_counts

            if self.num_signals % 2 != 0:
                packet += b'\x00'  # padding

            crc = crc16_ccitt(packet)
            packet += struct.pack('<H', crc)

            for receiver in self.receivers:
                self.sock.sendto(packet, receiver)

            self.packet_id += 1
            self.packets_sent += 1

            if self.num_packets_to_send != 0 and self.packets_sent >= self.num_packets_to_send:
                print("[INFO] Všechny požadované pakety odeslány.")
                self.sampling = False  # automaticky zastavit sampling

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

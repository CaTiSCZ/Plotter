import socket
import threading
import time
import struct
import numpy as np
import argparse
from collections import deque
from queue import Queue, Empty

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

ACK_packet = 0
ID_packet = 1
DATA_packet = 2
TRIGGER_packet = 3

#CMD
PING = 0
GET_ID = 1
REGISTER_RECEIVER = 2
REMOVE_RECEIVER = 3
GET_RECEIVERS =	4
START_SAMPLING = 5
START_ON_TRIGGER = 6
STOP_SAMPLING = 7
TRIGGER_ACK = 8
FORSE_TRIGGER =	9
#127.0.0.1:9999

class MultiSignalTestGenerator:
    def __init__(self, ip='127.0.0.1', port=10578, interval=0.001, num_signals = 1, print_interval = 1):
        self.ip = ip
        self.port = port
        self.interval = interval
        self.print_interval = print_interval
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
        self.wait_for_trigger = False
        self.wait_for_response = False
        self.print_queue = Queue()

    def start(self):
        self.running = True 
        self.listener_thread = threading.Thread(target=self._listen_for_command)
        self.listener_thread.start()

    def stop(self):
        self.running = False
        if hasattr(self, 'listener_thread'):
            self.listener_thread.join()
        self.sock.close()
        while not self.print_queue.empty():
            print(*self.print_queue.get())

    def print(self, *msg):
        self.print_queue.put(msg)

    def pop_msg(self, timeout=None):
        item = self.print_queue.get(timeout=timeout)
        self.print_queue.task_done()
        return item

    def _listen_for_command(self):
        self.print("Čekám na příkazový paket...")
        while self.running:
            try:
                cmd_data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                self._response() #TODO: dořešit umístění pro edge case neustlého přijmu cmd
                continue

            if len(cmd_data) < 4:
                continue  # Příliš krátký paket, ignoruj

            command_type = struct.unpack('<I', cmd_data[:4])[0]

            if command_type == PING:
                self.print("Přijat ping.")
                response = struct.pack('<HHI', ACK_packet, 0, command_type)  # Packet type, error, CMD
                self.sock.sendto(response, addr)

            elif command_type == GET_ID:
                self._send_identification_packet(addr)
                
            elif command_type == REGISTER_RECEIVER:
                self._register_receiver(cmd_data, addr)

            elif command_type == REMOVE_RECEIVER:
                self._remove_receiver(cmd_data, addr)

            elif command_type == GET_RECEIVERS:
                self._send_receivers_list(addr)

            elif command_type == START_SAMPLING or command_type == START_ON_TRIGGER: 
                command_type = struct.unpack('<I', cmd_data[:4])[0]
                if len(cmd_data) < 8:
                    self.print(f"⚠️ CMD {command_type} má nedostatečnou délku.")
                    
                _, num_packets = struct.unpack('<II', cmd_data)
                self.print(f"Přijat příkaz: typ={command_type}, počet paketů={num_packets}")

                self.num_packets_to_send = num_packets
                
                if command_type == START_SAMPLING:
                    self._start_sampling()
                else:
                    self.print("[INFO] Sampling spuštěn na trigger.")   
                    self.wait_for_trigger = True
             
                response = struct.pack('<HHIQ', ACK_packet, 0, command_type, self.num_packets_to_send)
                self.sock.sendto(response, addr)
            
            elif command_type == STOP_SAMPLING:
                # Stop sampling
                self.sampling = False
                self.print(f"[INFO] Sampling zastaven, odesláno paketů: {self.packets_sent}")

                # Odpověď: ACK + CMD + počet odeslaných paketů
                response = struct.pack('<HHIQ', ACK_packet, 0, command_type, self.packets_sent)
                self.sock.sendto(response, addr)

            elif command_type == TRIGGER_ACK:
                self.print("[INFO] Přijat Trigger ACK(CMD 8)")
                self.wait_for_response = False

            elif command_type == FORSE_TRIGGER:
                self.wait_for_trigger == False
                self._trigger()

            else:
                self.print(f"Neznámý příkaz typu {command_type}")

    def _send_identification_packet(self, addr):
        packet_type = ID_packet
        state = 0
        hw_id = 0x1234
        hw_ver_major = 1
        hw_ver_minor = 0
        hw_mcu_serial = 0x11223344
        cpu_uid = (0xAABBCCDD, 0xEEFF0011, 0x22334455)
        adc_hw_id = 0x5678
        adc_ver_major = 1
        adc_ver_minor = 1
        adc_serial = 0x99AABBCC
        fw_id = 0xABCD
        fw_ver_major = 2
        fw_ver_minor = 3
        fw_config = b"RELEASE\0"  # 8 bajtů, včetně ukončovací nuly
        build_time = b"2025-05-13T12:43:13\0".ljust(30, b'\x00')
        channels_count = self.num_signals

        # Hlavička
        header = struct.pack(
            '<HHHBBI3I HBB I HBB 8s 30s H',
            packet_type,
            state,
            hw_id,
            hw_ver_major,
            hw_ver_minor,
            hw_mcu_serial,
            *cpu_uid,
            adc_hw_id,
            adc_ver_major,
            adc_ver_minor,
            adc_serial,
            fw_id,
            fw_ver_major,
            fw_ver_minor,
            fw_config,
            build_time,
            channels_count
        )

        # Jednotky, offsety, zisky (12 bajtů na kanál)
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

        # Spojení hlavičky a dat
        full_packet = header + units_and_gains

        # CRC
        crc = crc16_ccitt(full_packet)
        full_packet += struct.pack('<H', crc)

        self.sock.sendto(full_packet, addr)

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
            self.print(f"Registrován nový přijímač: {receiver}")
        else:
            self.print(f"Přijímač už existuje: {receiver}")

        index = self.receivers.index(receiver)

        # Odpověď: ACK + CMD + IP + Port + index
        response = struct.pack('<HHI4sHB',
            ACK_packet,         # packet type (ACK)
            0,                  # error/state
            REGISTER_RECEIVER,  # CMD
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
            self.print(f"Odstraněn přijímač: {receiver}")
        else:
            self.print(f"Přijímač nenalezen: {receiver}")

        response = struct.pack('<HHI', ACK_packet, 0, REMOVE_RECEIVER)
        self.sock.sendto(response, addr)

    def _send_receivers_list(self, addr):
        response = struct.pack('<HHI', 0, 0, 4)

        for ip, port in self.receivers:
            ip_bytes = socket.inet_aton(ip)
            response += ip_bytes
            response += struct.pack('<H', port)

        self.sock.sendto(response, addr)

    def _start_sampling(self):
        
        self.packets_sent = 0  # reset počítadla
        self.sampling = True  # flag spuštění samplingu

        # Spustit odesílání dat v samostatném vlákně, pokud ještě neběží
        if self.sender_thread is None or not self.sender_thread.is_alive():
            self.sender_thread = threading.Thread(target=self._send_data_to_all_receivers, daemon=True)
            self.sender_thread.start()

    def _trigger(self):
        self.print("Přijat trigger")
        if self.sampling == True:
            self._trigger_packet()
            
        elif self.wait_for_trigger == True:
            self._start_sampling()
            self._trigger_packet()
        
        else:
            self._trigger_packet()


    def _trigger_packet(self):
        self.wait_for_response = True
        self.response_count = 0
        
        self.trigger_packet = struct.pack('<HHB', TRIGGER_packet, self.packet_id, 0)
        for receiver in self.receivers:
            self.sock.sendto(self.trigger_packet, receiver)
            self.print("odeslán trigger packet")

    def _response(self):
        if self.wait_for_response == True:
            for receiver in self.receivers:
                self.sock.sendto(self.trigger_packet, receiver)
            self.response_count += 1
            if self.response_count == 10:
                self.wait_for_response == False
                self.print("odesílání trigger packet selhalo")
        

    def _send_data_to_all_receivers(self):
        
        period_length = 200000
        packet_size = 200
        base_signal = np.linspace(-32768, 32767, period_length, dtype=np.int16)
        
        self.print("[INFO] Zahájeno odesílání dat...")

        t0 = time.monotonic()
        t_send = t0
        t_print = t0
        last_sent = 0

        while self.running:         
            
            if not self.sampling:
                time.sleep(0.1)
                t0 = time.monotonic()
                t_send = t0
                t_print = t0
                last_sent = 0
                continue

            if not self.receivers:
                self.print("[WAIT] Žádní příjemci. Čekám...")
                time.sleep(1)
                t0 = time.monotonic()
                t_send = t0
                t_print = t0
                last_sent = 0
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
            header = struct.pack('<HH', DATA_packet, self.packet_id % 65536)
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
                self.print(f"[INFO] Všechny požadované pakety odeslány ({self.packets_sent} / {self.num_packets_to_send}).")
                self.sampling = False  # automaticky zastavit sampling

            t = time.monotonic()
            t_send += self.interval
            pause = t_send - t
            if t >= t_print:
                t_print += self.print_interval
                self.print(f"{t-t0:10.3f}: sent {self.packets_sent:12} packets ({(self.packets_sent - last_sent) // self.print_interval} packets / s), delay is {pause:9.6f} seconds")
                last_sent = self.packets_sent
            if pause > 0:
                time.sleep(pause)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Testovací UDP generátor více signálů")
    parser.add_argument('--signals', type=int, default=2, help="Počet signálů v jednom packetu")
    args = parser.parse_args()

    gen = MultiSignalTestGenerator(num_signals=args.signals)
    gen.start()
    print(f"Generátor připraven (signálů: {args.signals}). Čekám na příkaz.")

    while True:
        try:
            print(*gen.pop_msg(0.1))
        except Empty:
            pass
        except KeyboardInterrupt:
            print("\nUkončuji...")
            gen.stop()
            break

import logging
from logger import application_logger
from async_socket import AsyncSocket
import threading
import struct
from enum import IntEnum
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
import socket

    # ---------------------- CMD a packety -------------------
class PACKET(IntEnum):
    ACK_packet = 0
    ID_packet = 1
    DATA_packet = 2
    TRIGGER_packet = 3

class CMD(IntEnum):
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

class STRUCT:
    HEADER = struct.Struct("<HH")
    CMD = struct.Struct("<I")
    CRC = struct.Struct("<H")
    ID = struct.Struct('<HBBI3I HBB I HBB 8s 30s H')
    CHANNEL = struct.Struct('<4s ff')


class Device:
    @dataclass
    class CommandRecord:
        timer: threading.Timer
        on_timeout: Callable[[CMD, list[Any], dict[str: Any]], None]
        on_timeout_args: list[Any]
        on_timeout_kwargs: dict[str: Any]
        on_ack: Callable[[CMD, int, bytes, list[Any], dict[str: Any]], None]
        on_ack_args: list[Any]
        on_ack_kwargs: dict[str: Any]

    @dataclass    
    class ChannelInfo:
        unit: str
        offset: float
        gain: float    
    
    @staticmethod
    def _crc16_ccitt(data: memoryview, start, end, poly=0x1021, crc=0xFFFF):
        for i in range (start, end):
            crc ^= data[i] << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ poly
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    @staticmethod
    def _verify_crc(packet: memoryview):
        if (l:=len(packet)) < STRUCT.CRC.size:
            return None
        received_crc = STRUCT.CRC.unpack_from(packet, l - STRUCT.CRC.size)[0]
        expected_crc = Device._crc16_ccitt(packet, 0, l- STRUCT.CRC.size)
        return True if expected_crc == received_crc else (received_crc, expected_crc)

    def __init__(self, cmd_socket, addr):
        self._logger = logging.getLogger(__class__.__name__ if application_logger is None else f'{application_logger}.{__class__.__name__}')
        
        self.cmd_socket = cmd_socket
        self.addr = addr

        self.sent_commands = {}
        self.timeout = 1

        self.send_command_lock = threading.Lock()
    
    def _keep_alive (self):
        pass

    def packet_received (self, packet):
        min_lenght = STRUCT.HEADER.size
        if (packet_lenght:=len(packet)) < min_lenght:
            self._logger.warning(f"Packet received: Too short packet ({packet_lenght}).")
            return
        packet_view = memoryview(packet)
        packet_type, error = STRUCT.HEADER.unpack_from(packet, 0)
        match packet_type:
            case PACKET.ACK_packet:
                offset = min_lenght
                min_lenght += STRUCT.CMD.size
                if packet_lenght < min_lenght:
                    self._logger.warning(f"Packet received: Too short ACK packet ({packet_lenght}).")
                    return  
                cmd = STRUCT.CMD.unpack_from(packet, offset)[0]
                record = self._cancel_timeout(cmd)
                record.on_ack(cmd, error, packet_view[min_lenght:], *record.on_ack_args, **record.on_ack_kwargs)

            case PACKET.ID_packet:
                cmd = CMD.GET_ID
                record = self._cancel_timeout(cmd)
                match Device._verify_crc(packet_view):
                    case True:
                        pass
                    case None:
                        self._logger.error(f"Packet received: Too short ID packet ({packet_lenght}).")
                        return
                    case (received_crc, expected_crc):
                        self._logger.error(f"Packet received: ID packet with invalid CRC; reveived: {received_crc}, expected: {expected_crc}")
                        return
                offset = min_lenght
                min_lenght += STRUCT.ID.size
                if packet_lenght < min_lenght:
                    self._logger.error(f"Packet received: Too short ID packet ({packet_lenght}).")
                    return
                unpacked = STRUCT.ID.unpack_from(packet_view[offset : min_lenght])
                self.id = {
                    'mcu_hw_id': unpacked[0],
                    'mcu_hw_ver_major': unpacked[1],
                    'mcu_hw_ver_minor': unpacked[2],
                    'mcu_serial': unpacked[3],
                    'cpu_uid': (unpacked[4], unpacked[5], unpacked[6]),
                    'adc_hw_id': unpacked[7],
                    'adc_ver_major': unpacked[8],
                    'adc_ver_minor': unpacked[9],
                    'adc_serial': unpacked[10],
                    'fw_id': unpacked[11],
                    'fw_ver_major': unpacked[12],
                    'fw_ver_minor': unpacked[13],
                    'fw_config': unpacked[14].decode('ascii').rstrip('\x00'),
                    'build_time': unpacked[15].decode('ascii').rstrip('\x00'),
                    'channels_count': unpacked[16],
                }
                self.channels_count = self.id['channels_count']
                offset = min_lenght
                min_lenght += self.channels_count * STRUCT.CHANNEL.size
                if len(packet) < min_lenght:
                    self._logger.error(f"Packet received: Too short ID packet ({packet_lenght}).")
                    return
                self.channel_info = []
                unpacker = STRUCT.CHANNEL.iter_unpack(packet_view[offset:])
                for i in range (self.channels_count):
                    info = Device.ChannelInfo(*next(unpacker))
                    info.unit = info.unit.decode('ascii').rstrip('\x00')
                    self.channel_info.append(info)
                self._logger.info(f"Packet received: ID packet received")    
                    
    def _timeout(self, cmd):
        with self.send_command_lock:
            record = self.sent_commands[cmd]
            del self.sent_commands[cmd]
        record.on_timeout(cmd, *record.on_timeout_args, **record.on_timeout_kwargs)
    
    def _cancel_timeout(self, cmd):
        try:
            with self.send_command_lock:
                record = self.sent_commands[cmd]
                record.timer.cancel()
                del self.sent_commands[cmd]
        except KeyError:
            self._logger.info("Packet received: unexpected response for {cmd}")
        return record        

    def on_timeout(self, cmd):
        self._logger.warning(f"Send command: no answer for {cmd}" )
        
    def send_command(self, cmd: int, data: bytes = b'', on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        with self.send_command_lock:
            if on_timeout is not None and on_ack is not None:
                if cmd in self.sent_commands:
                    self._logger.warning(f"Send command: Command {cmd} in progres.")
                    return
                self.sent_commands[cmd] = Device.CommandRecord(threading.Timer(self.timeout, self._timeout),
                                                               on_timeout, on_timeout_args, on_timeout_kwargs,
                                                               on_ack, on_ack_args, on_ack_kwargs)

            packet = struct.pack('<I', cmd) + data
            self.cmd_socket.sendto(packet, self.addr)

    def ping(self):       
        try:
            self.send_command(CMD.PING, on_timeout = self.on_timeout, on_ack = lambda cmd, error, data: self._logger.info(f"Ping: ok") )
        except Exception as e:
            self._logger.error(f"Ping: {e}")

    def get_id(self):
        try: 
            self.send_command(CMD.GET_ID, on_timeout = self.on_timeout)
        except Exception as e:
            self._logger.error(f"Get ID: {e}")

    def register_receiver(self, addr):
        def on_ack(cmd, error, data):
            if len(data) < 8:
                self._logger.error(f"Register receiver: ACK to short ({len(data)} bytes)")
                return  
            ip = socket.inet_ntoa(data[:4])
            port, order = struct.unpack('<HB', data[4:])
            self._logger.info(
                f"Register receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
                f"Order: {order}"
            )
        try:
            addr, port = addr.rsplit(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<H', int(port))
            self.send_command(CMD.REGISTER_RECEIVER, data, on_timeout = self.on_timeout, on_ack = on_ack )
        except Exception as e:
            self._logger.error(f"Registr receiver: {e}")

    def connect(self):
        self.get_id()
        self.register_receiver()

    def remove_receiver(self):
        def on_ack(cmd, error, data):
            if len(data) < 7:
                self._logger.error(f"Remove receiver: ACK to short ({len(data)} bytes)")
                return  
            ip = socket.inet_ntoa(data[:4])
            port = struct.unpack('<H', data[4:])
            self._logger.info(
                f"Remove receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
            )
        try:
            addr, port = addr.rsplit(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<H', int(port))
            self.send_command(CMD.REMOVE_RECEIVER, data, on_timeout = self.on_timeout, on_ack = on_ack )
        except Exception as e:
            self._logger.error(f"Remove receiver: {e}")
    
    def get_receivers(self):
        def on_ack(cmd, error, data):
            if len(data) % 6 != 0:
                self._logger.warning(f"Get receivers: ACK incomplete ({len(data)} bytes)") 
            receivers = []
            offset = 0
            while offset + 6 <= len(data):
                ip = socket.inet_ntoa(data[offset:offset + 4])
                port = struct.unpack('<H', data[offset + 4:offset + 6])[0]
                receivers.append(f"{ip}:{port}")
                offset += 6
            if receivers:
                self._logger.info("Registred receivers:\n" + "\n".join(receivers))
            else:
                self._logger.info("Get receivers: no registred receivers")
        try:
            self.send_command(CMD.GET_RECEIVERS, on_timeout = self.on_timeout, on_ack = on_ack )
        except Exception as e:
            self._logger.error(f"Get receivers: {e}")
    
    def start_sampling(self):
        self.sampling_thread.received_packets = 0
        self.num_packets = self.num_packets_spinbox.value()
        data = struct.pack('<I', self.num_packets)
        if self.channels_count == 0:
            self._logger.error("Need Get ID at first")
            return

        resp = self.send_command(CMD.START_SAMPLING, data, expect_response=True)

        self._logger.info(f"Start sampling, {self.num_packets} packets")

    def start_on_trigger(self):
            self.sampling_thread.received_packets = 0
            self.num_packets = self.num_packets_spinbox.value()
            data = struct.pack('<I', self.num_packets)
            if self.channels_count == 0:
                self._logger.error("Need Get ID at first")
                return

            resp = self.send_command(CMD.START_ON_TRIGGER, data, expect_response=True)

            self._logger.info(f"Waiting on trigger, {self.num_packets} packets")

    def stop_sampling(self):
        if self.sampling_thread and self.sampling_thread.isRunning():
            self._logger.info(f"Stop sampling, received packets: {self.sampling_thread.received_packets}")
            resp = self.send_command(CMD.STOP_SAMPLING, expect_response=True)
            
            if resp and len(resp) >= 16:  # 2+2+4+8 = 16 bajtů
                packet_type, error_state, cmd_type, packets_sent = struct.unpack('<HHIQ', resp[:16])
                
                if packet_type == PACKET.ACK_packet and cmd_type == CMD.STOP_SAMPLING:
                    self._logger.debug(f"Stop sampling confirmed, packets sent by divice: {packets_sent}")
                    if packets_sent != self.sampling_thread.received_packets:
                        self._logger.warning(f"Packet from device ({packets_sent}) not equal to recv packets ({self.sampling_thread.received_packets}) ")
                else:
                    self._logger.warning(f"Stop sampling: unexpected ACK structure or CMD")
            else:
                self._logger.warning(f"Stop sampling: no or invalid ACK response")
            
            self.sampling_thread.flush_packet_buffer()
            self.update_plot_buffered()
        else:
            self._logger.info("Sampling is already stopped")
 
    def send_trigger(self):
        try:
            resp = self.send_command(CMD.FORSE_TRIGGER)
            self._logger.info("Sent trigger (CMD 9)")
        
        except Exception as e:
            self._logger.error(f"Trigger send: {e}")
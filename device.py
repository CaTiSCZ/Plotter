import logging
from logger import application_logger
import threading
import struct
from enum import IntEnum
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
import socket
from event import Event
import numpy as np

    # ---------------------- CMD a packety -------------------
class PACKET(IntEnum):
    ACK_packet              =  0
    ID_packet               =  1
    DATA_packet             =  2
    TRIGGER_packet          =  3
    LOG_packet              =  4

class CMD(IntEnum):
    PING                    =  0
    GET_ID                  =  1
    REGISTER_RECEIVER       =  2
    REMOVE_RECEIVER         =  3
    GET_RECEIVERS           =  4
    START_SAMPLING          =  5
    START_ON_TRIGGER        =  6
    STOP_SAMPLING           =  7
    TRIGGER_ACK             =  8
    FORCE_TRIGGER           =  9
    RESET_COUNTERS          = 10
    CLOCK_CONFIG            = 11
    GET_ACQUISITION_STATE   = 12
    DEVICE_ID_CONFIG        = 13
    RESET_DEVICE            = 14
    GET_CLOCK_CONFIG        = 15

class STRUCT:
    HEADER = struct.Struct("<HH")
    CMD = struct.Struct("<I")
    CRC = struct.Struct("<H")
    ID = struct.Struct('<HBBI3I HBB I HBB 8s 30s H')
    CHANNEL = struct.Struct('<4s ff')

SAVE_KEY = 0xAC
RESET_KEY = 0xFE

class Device:

    RAW_DATA_TYPE = np.int16
    SAMPLES_PER_PACKET = 200
    PACKETS_PER_SECOND = 1000
    DEFAULT_BUFFER_SIZE = 10 * SAMPLES_PER_PACKET * PACKETS_PER_SECOND
    BUFFER_EXTEND = 3


    @dataclass
    class CommandRecord:
        timer: threading.Timer
        on_timeout: Callable[[CMD, list[Any], dict[str, Any]], None] | None
        on_timeout_args: list[Any]
        on_timeout_kwargs: dict[str, Any]
        on_ack: Callable[[CMD, int, bytes, list[Any], dict[str, Any]], None] | None
        on_ack_args: list[Any]
        on_ack_kwargs: dict[str, Any]

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
        self.buffer_lock = threading.Lock()

        self.is_sampling = False
        self.id = {}
        self.channels_count = None
        self.channel_info = []
        self.buffer_size = Device.DEFAULT_BUFFER_SIZE


        self.event_ACK = Event()
        self.event_ID = Event()
        self.event_data = Event()
        self.event_trigger = Event()
        self.event_log = Event()
        self.event_shrink_buffer = Event()


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
                if record is not None and record.on_ack is not None:
                    record.on_ack(cmd, error, packet_view[min_lenght:], *record.on_ack_args, **record.on_ack_kwargs)
                self.event_ACK.emit(self, cmd, error, packet_view[min_lenght:])

            case PACKET.ID_packet:
                cmd = CMD.GET_ID
                record = self._cancel_timeout(cmd)
                match Device._verify_crc(packet_view):
                    case True:
                        pass
                    case None:
                        self._logger.error(f"Packet received: Too short ID packet ({packet_lenght}).")
                        if record is not None and record.on_timeout is not None:
                            record.on_timeout(*record.on_timeout_args, **record.on_timeout_kwargs)
                        return
                    case (received_crc, expected_crc):
                        self._logger.error(f"Packet received: ID packet with invalid CRC; reveived: {received_crc}, expected: {expected_crc}")
                        if record is not None and record.on_timeout is not None:
                            record.on_timeout(*record.on_timeout_args, **record.on_timeout_kwargs)
                        return
                offset = min_lenght
                min_lenght += STRUCT.ID.size
                if packet_lenght < min_lenght:
                    self._logger.error(f"Packet received: Too short ID packet ({packet_lenght}).")
                    if record is not None and record.on_timeout is not None:
                        record.on_timeout(*record.on_timeout_args, **record.on_timeout_kwargs)
                    return
                unpacked = STRUCT.ID.unpack_from(packet_view[offset : min_lenght])
                old_id = self.id
                
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
                channels_count = self.id['channels_count']
                offset = min_lenght
                min_lenght += channels_count * STRUCT.CHANNEL.size
                if len(packet) < min_lenght:
                    self._logger.error(f"Packet received: Too short ID packet ({packet_lenght}).")
                    if record is not None and record.on_timeout is not None:
                        record.on_timeout(*record.on_timeout_args, **record.on_timeout_kwargs)
                    return
                old_channel_info = self.channel_info
                self.channel_info = []
                unpacker = STRUCT.CHANNEL.iter_unpack(packet_view[offset:min_lenght])
                for i in range (channels_count):
                    info = Device.ChannelInfo(*next(unpacker))
                    info.unit = info.unit.decode('ascii').rstrip('\x00')
                    self.channel_info.append(info)
                self._logger.info(f"Packet received: ID packet received")
                #volat funkci zajišťující správný přepočet dat
                self._init_buffer(channels_count = channels_count)
                if record is not None and record.on_ack is not None:
                    record.on_ack(*record.on_ack_args, **record.on_ack_kwargs)
                self.event_ID.emit(self, error, old_id, self.id, old_channel_info, self.channel_info)

            case PACKET.DATA_packet:
                self.event_data.emit(self, error, packet_view[min_lenght:])
            case PACKET.TRIGGER_packet:
                self._logger.info(f"Packet received: TRIGGER packet received")
                self.event_trigger.emit(self, error, packet_view[min_lenght:])
            case PACKET.LOG_packet:
                packet_num = error
                msg = packet[min_lenght:].decode('ascii').rstrip('\x00')
                self._logger.info(f"LOG [{packet_num:5}]: {msg}")
                self.event_log.emit(self, packet_num, msg)

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
                return record 
        except KeyError:
            self._logger.info("Packet received: unexpected response for {cmd}")
        return None

    def on_timeout(self, cmd, msg = None, on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}):
        self._logger.warning(msg or f"Send command: no answer for {cmd}" )
        if on_timeout is not None:
            on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def on_ack(self, cmd, error, data, msg = None, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        self._logger.info(msg or f"Send command: received ACK for {cmd}")
        if on_ack is not None:
            on_ack(error, data, *on_ack_args, **on_ack_kwargs)

    def _init_buffer(self, *, channels_count = None, buffer_size = None):
        with self.buffer_lock:
            if buffer_size is None and channels_count is None:
                self._logger.critical("Init buffer: Both channels_count and buffer_size cannot be None")
                raise ValueError("Both channels_count and buffer_size cannot be None")
            elif channels_count is not None:
                if buffer_size is not None:
                    self._logger.critical("Init buffer: Both channels_count and buffer_size cannot change")
                    raise ValueError("Both channels_count and buffer_size cannot change")
                elif self.channels_count is None:
                    self.raw_buffer = np.zeros((channels_count, Device.BUFFER_EXTEND * self.buffer_size), dtype=Device.RAW_DATA_TYPE)
                elif channels_count > self.channels_count:
                    self.raw_buffer = np.concatenate((self.raw_buffer, np.zeros((channels_count - self.channels_count, Device.BUFFER_EXTEND * self.buffer_size), dtype=Device.RAW_DATA_TYPE)), axis=0)
                elif channels_count < self.channels_count:
                    raw = self.raw_buffer
                    self.event_shrink_buffer.emit(self, raw)
                    self.raw_buffer = self.raw_buffer[:channels_count]
                self.channels_count = channels_count
            elif buffer_size is not None:
                if self.channels_count is not None:
                    if buffer_size > self.buffer_size:
                        self.raw_buffer = np.concatenate((self.raw_buffer, np.zeros((self.channels_count, Device.BUFFER_EXTEND * (buffer_size - self.buffer_size)), dtype=Device.RAW_DATA_TYPE)), axis=1)
                    elif buffer_size < self.buffer_size:
                        raw = self.raw_buffer
                        self.event_shrink_buffer.emit(self, raw)
                        self.raw_buffer = self.raw_buffer[:, :Device.BUFFER_EXTEND * buffer_size]
                self.buffer_size = buffer_size


    def send_command(self, cmd: int, data: bytes = b'', on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        with self.send_command_lock:
            if on_timeout is not None or on_ack is not None:
                if cmd in self.sent_commands:
                    self._logger.warning(f"Send command: Command {cmd} in progres.")
                    return
                self.sent_commands[cmd] = Device.CommandRecord(threading.Timer(self.timeout, self._timeout),
                                                               on_timeout, on_timeout_args, on_timeout_kwargs,
                                                               on_ack, on_ack_args, on_ack_kwargs)

            packet = struct.pack('<I', cmd) + data
            self.cmd_socket.sendto(packet, self.addr)

    def ping(self, on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):       
        try:
            self.send_command(CMD.PING, 
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = self.on_ack, on_ack_args=("Ping: ok", on_ack, on_ack_args, on_ack_kwargs))
        except Exception as e:
            self._logger.error(f"Ping: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def get_id(self, on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        try: 
            self.send_command(CMD.GET_ID,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = on_ack, on_ack_args=on_ack_args, on_ack_kwargs=on_ack_kwargs)
        except Exception as e:
            self._logger.error(f"Get ID: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def register_receiver(self, addr, source, on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        def _on_ack(cmd, error, data):
            if len(data) < 7:
                self._logger.error(f"Register receiver: ACK to short ({len(data)} bytes)")
                if on_timeout is not None:
                    on_timeout(*on_timeout_args, **on_timeout_kwargs)
                return  
            ip = socket.inet_ntoa(data[:4])
            port, order = struct.unpack('<HB', data[4:])
            self._logger.info(
                f"Register receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
                f"Order: {order}"
            )
            if on_ack is not None:
                on_ack(error, ip, port, order, *on_ack_args, **on_ack_kwargs)
        try:
            addr, port = addr.rsplit(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<HB', int(port), source)
            self.send_command(CMD.REGISTER_RECEIVER, data,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = _on_ack)
        except Exception as e:
            self._logger.error(f"Register receiver: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def remove_receiver(self, addr, source, on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        def _on_ack(cmd, error, data):
            if len(data) < 6:
                self._logger.error(f"Remove receiver: ACK to short ({len(data)} bytes)")
                if on_timeout is not None:
                    on_timeout(*on_timeout_args, **on_timeout_kwargs)
                return  
            ip = socket.inet_ntoa(data[:4])
            port = struct.unpack('<H', data[4:])
            self._logger.info(
                f"Remove receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
            )
            if on_ack is not None:
                on_ack(error, ip, port, *on_ack_args, **on_ack_kwargs)
        try:
            addr, port = addr.rsplit(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<HB', int(port), source)
            self.send_command(CMD.REMOVE_RECEIVER, data,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = _on_ack)
        except Exception as e:
            self._logger.error(f"Remove receiver: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def get_receivers(self, source, on_timeout = None, on_timeout_args = [], on_timeout_kwargs = {}, on_ack = None, on_ack_args = [], on_ack_kwargs = {}):
        def _on_ack(cmd, error, data):
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
                self._logger.info("Registered receivers:\n" + "\n".join(receivers))
            else:
                self._logger.info("Get receivers: no registered receivers")
            if on_ack is not None:
                on_ack(error, receivers, *on_ack_args, **on_ack_kwargs)
        try:
            self.send_command(CMD.GET_RECEIVERS, struct.pack('<B', source),
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = _on_ack )
        except Exception as e:
            self._logger.error(f"Get receivers: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def _start_sampling(self, name, cmd, packet_count, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        def _on_ack(cmd, error, data):
            requested_packets = struct.unpack_from('<I', data, 0)[0]
            if self.packet_count != requested_packets:
                self._logger.warning(f"{name}: sent count ({self.packet_count}) not equal to received count ({requested_packets})")
            else:
                self._logger.info(f"{name}: sent packets count: {self.packet_count}")
            self.is_sampling = True
            if on_ack is not None:
                on_ack(error, requested_packets, *on_ack_args, **on_ack_kwargs)
        self.received_packets = 0
        self.packet_count = packet_count
        data = struct.pack('<I', self.packet_count)
        if self.channels_count == 0:
            def repeat():
                if self.channels_count == 0:
                    self._logger.error(f"{name}: device with 0 channels")
                    if on_timeout is not None:
                        on_timeout(*on_timeout_args, **on_timeout_kwargs)
                else: 
                    self._start_sampling(name, cmd, packet_count, on_timeout, on_timeout_args, on_timeout_kwargs, on_ack, on_ack_args, on_ack_kwargs)
            msg = f"{name}: get id failed"
            self.get_id(on_timeout = self.on_timeout, on_timeout_args = (msg, on_timeout, on_timeout_args, on_timeout_kwargs), 
                        on_ack = repeat)
            return

        try:
            self.send_command(cmd, data, 
                              on_timeout = self.on_timeout, on_timeout_args = (None, on_timeout, on_timeout_args, on_timeout_kwargs), 
                              on_ack = _on_ack)
        except Exception as e:
            self._logger.error(f"{name}: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def start_sampling(self, packet_count, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        self._start_sampling("Start sampling", CMD.START_SAMPLING, packet_count, on_timeout, on_timeout_args, on_timeout_kwargs, on_ack, on_ack_args, on_ack_kwargs)

    def start_on_trigger(self, packet_count, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        self._start_sampling("Start on trigger", CMD.START_ON_TRIGGER, packet_count, on_timeout, on_timeout_args, on_timeout_kwargs, on_ack, on_ack_args, on_ack_kwargs)

    def stop_sampling(self, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        if self.is_sampling:
            def _on_ack(cmd, error, data):
                sent_packets = struct.unpack_from('<I', data, 0)[0]
                if self.received_packets != sent_packets:
                    self._logger.warning(f"Stop sampling: received packets ({self.received_packets}) not equal to sent packets ({sent_packets})")
                else:
                    self._logger.info(f"Stop sampling acknowledged, sent packets: {sent_packets}")
                self.is_sampling = False
                if on_ack is not None:
                    on_ack(error, sent_packets, *on_ack_args, **on_ack_kwargs)
            try:
                self.send_command(CMD.STOP_SAMPLING, 
                                  on_timeout = self.on_timeout, on_timeout_args = (None, on_timeout, on_timeout_args, on_timeout_kwargs),
                                  on_ack = _on_ack)
            except Exception as e:
                self._logger.error(f"Stop sampling failed: {e}")
                if on_timeout is not None:
                    on_timeout(*on_timeout_args, **on_timeout_kwargs)
        else:
            self._logger.info("Sampling is not running")

    def send_trigger(self):
        try:
            self.send_command(CMD.FORCE_TRIGGER)
            self._logger.info("Sent trigger")
        except Exception as e:
            self._logger.error(f"Trigger send: {e}")

    def reset_counters(self, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        try:
            self.send_command(CMD.RESET_COUNTERS, 
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = self.on_ack, on_ack_args=(None, on_ack, on_ack_args, on_ack_kwargs))
        except Exception as e:
            self._logger.error(f"Reset counters: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def clock_config(self, external_clock: bool, clock_output: bool, save: bool, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        def _on_ack(cmd, error, data):
            if len(data) < 2:
                self._logger.error(f"Clock config: ACK to short ({len(data)} bytes)")
                if on_timeout is not None:
                    on_timeout(*on_timeout_args, **on_timeout_kwargs)
                return
            old_cfg, new_cfg = struct.unpack_from('<BB', data)
            self._logger.info(
                f"Clock config:\n"
                f"Old config: {old_cfg:02X}\n"
                f"New config: {new_cfg:02X}"
            )
            if on_ack is not None:
                on_ack(error, old_cfg, new_cfg, *on_ack_args, **on_ack_kwargs)
        try:
            data = struct.pack('<BB', 
                               (1 if external_clock else 0) | 
                               (2 if clock_output   else 0), 
                               SAVE_KEY if save else 0)
            self.send_command(CMD.CLOCK_CONFIG, data,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = _on_ack)
        except Exception as e:
            self._logger.error(f"Clock config: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def get_acquisition_state(self, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        def _on_ack(cmd, error, data):
            if len(data) < 9:
                self._logger.error(f"Get acquisition state: ACK to short ({len(data)} bytes)")
                if on_timeout is not None:
                    on_timeout(*on_timeout_args, **on_timeout_kwargs)
                return  
            acquired_samples, target_samples, state = struct.unpack_from('<IIB', data)
            self._logger.info(
                f"Get acquisition state:\n"
                f"Acquired samples: {acquired_samples}\n"
                f"Target samples: {target_samples}\n"
                f"State: {state:02X}"
            )
            if on_ack is not None:
                on_ack(error, acquired_samples, target_samples, state, *on_ack_args, **on_ack_kwargs)
        try:
            self.send_command(CMD.GET_ACQUISITION_STATE,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = _on_ack)
        except Exception as e:
            self._logger.error(f"Get acquisition state: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def device_id_config(self, device_id: int, save: bool, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        try:
            data = struct.pack('<IB', device_id, SAVE_KEY if save else 0)
            self.send_command(CMD.DEVICE_ID_CONFIG, data,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = self.on_ack, on_ack_args=(None, on_ack, on_ack_args, on_ack_kwargs))
        except Exception as e:
            self._logger.error(f"Device ID config: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def reset_device(self, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        try:
            self.send_command(CMD.RESET_DEVICE, struct.pack('<B', RESET_KEY),
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = self.on_ack, on_ack_args=(None, on_ack, on_ack_args, on_ack_kwargs))
        except Exception as e:
            self._logger.error(f"Reset device: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)

    def get_clock_config(self, on_timeout=None, on_timeout_args=[], on_timeout_kwargs={}, on_ack=None, on_ack_args=[], on_ack_kwargs={}):
        def _on_ack(cmd, error, data):
            if len(data) < 2:
                self._logger.error(f"Get clock config: ACK to short ({len(data)} bytes)")
                if on_timeout is not None:
                    on_timeout(*on_timeout_args, **on_timeout_kwargs)
                return
            active_cfg, stored_cfg = struct.unpack_from('<BB', data)
            self._logger.info(
                f"Get clock config:\n"
                f"Active config: {active_cfg:02X}\n"
                f"Stored config: {stored_cfg:02X}"
            )
            if on_ack is not None:
                on_ack(error, active_cfg, stored_cfg, *on_ack_args, **on_ack_kwargs)
        try:
            self.send_command(CMD.GET_CLOCK_CONFIG,
                              on_timeout = self.on_timeout, on_timeout_args=(None, on_timeout, on_timeout_args, on_timeout_kwargs),
                              on_ack = _on_ack)
        except Exception as e:
            self._logger.error(f"Get clock config: {e}")
            if on_timeout is not None:
                on_timeout(*on_timeout_args, **on_timeout_kwargs)
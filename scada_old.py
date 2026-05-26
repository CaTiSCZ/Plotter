# -*- coding: utf-8 -*-
"""multi_device_plotter.py – full-featured multi-device UDP client & visualiser
==============================================================================
Features:
  - Logging with timestamped pane
  - Enable/disable individual devices
  - Dynamic default IPs and ports based on first device entry
  - Automatic receiver address set from first device
  - Sample count input, Start/StopSampling and Trigger commands
  - Leader/follower selection (none = all followers)
  - Packet order checking per device
  - Display legend with device/channel colors
  - Clean Graf functionality
  - Save Data per device to CSV
  - Reset Counter command (code 10)
  - UDP I/O via selector-based asyncio loop (Windows compatible)
"""
from __future__ import annotations
import logging
import logger
import importlib
import asyncio, struct, socket, sys, time, threading, csv, os, tempfile
from collections import deque
from dataclasses import dataclass
from typing import Dict, Tuple, List
from datetime import datetime
from contextlib import ExitStack

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QSpinBox, QCheckBox, QTextEdit,
    QScrollArea, QRadioButton, QButtonGroup, QFileDialog, QMessageBox, QComboBox
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

APPLICATION_NAME = 'Eaton FDDS SCADA'
APPLICATION_VERSION = '1.5.0'
APPLICATION_TITLE = f"{APPLICATION_NAME} v{APPLICATION_VERSION}"

# Constants
DEFAULT_CMD_PORT   = 10578
DEFAULT_DATA_PORT  = 10580
RECV_TIMEOUT_S     = 0.3
SAMPLES_PER_PACKET = 200
PACKET_RATE_HZ     = 1000
SAMPLING_PERIOD    = 1/(SAMPLES_PER_PACKET*PACKET_RATE_HZ)
PACKET_PERIOD      = 1/(PACKET_RATE_HZ)
BUFFER_LENGTH_S    = 30
BUFFER_SIZE        = int(BUFFER_LENGTH_S*SAMPLES_PER_PACKET*PACKET_RATE_HZ)
DEFAULT_AVG_LEN_MS = 1000 # could be overwritten by default_settings.py
CCU_DEVICE_INDEX   = 0
DEFAULT_SOCKET_BACKEND = 'auto'
DATA_SOCKET_RECV_TIMEOUT_S = 0.02
DATA_SOCKET_DRAIN_TIMEOUT_S = 0.3
PTP_TRIGGER_RING_PACKETS = 500

# Features
FCN_QT_LOGGING = True  # Enable Qt logging handler

CLOCK_SETTINGS = ['Internal isolated', 'Internal OUT', 'External', 'External OUT', 'PTP isolated', 'PTP OUT']


def _resolve_buffered_socket_class(backend: str):
    backend = (backend or DEFAULT_SOCKET_BACKEND).strip().lower()

    if backend in ('auto', 'cpp'):
        try:
            import cppimport
            mod = cppimport.imp('buffered_socket.buffered_socket_cpp')
            return mod.BufferedSocket, 'cpp'
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, GeneratorExit)):
                raise
            if backend == 'cpp':
                raise
            logging.getLogger(__name__).warning(
                f"C++ buffered socket import failed ({type(e).__name__}: {e}). Falling back to Python backend."
            )

    if backend in ('auto', 'py', 'python'):
        from buffered_socket_py import BufferedSocket as PyBufferedSocket
        return PyBufferedSocket, 'python'

    raise ValueError(f"Unknown socket backend '{backend}'. Expected one of: auto, cpp, py")

# CRC-16/CCITT checksum
try:
    import crcmod
    _crc16_func = crcmod.predefined.mkCrcFun('crc-ccitt-false')
    def crc16_ccitt(data: bytes, poly: int=0x1021, crc: int=0xFFFF) -> int:
        return _crc16_func(data)
except ImportError:
    def crc16_ccitt(data: bytes, poly: int=0x1021, crc: int=0xFFFF) -> int:
        for b in data:
            crc ^= b<<8
            for _ in range(8):
                crc = ((crc<<1)^poly)&0xFFFF if crc&0x8000 else (crc<<1)&0xFFFF
        return crc

CRC_STRUCT = struct.Struct('<H')

def _verify_crc(pkt: bytes) -> bytes|None:
    if not pkt or len(pkt)<2:
        return None
    data, recv_crc = pkt[:-2], CRC_STRUCT.unpack(pkt[-2:])[0]
    return data if crc16_ccitt(data)==recv_crc else False

# ID packet parsing
ID_HEADER_STRUCT = struct.Struct('<HH HBB HBBI3I HBBI HH')

def parse_id_packet(data):
    if len(data) < ID_HEADER_STRUCT.size:
        raise ValueError("[ERR]: ID packet is short")
    unpacked = ID_HEADER_STRUCT.unpack(data[:ID_HEADER_STRUCT.size])
    return {
        'packet_type': unpacked[0],
        'state': unpacked[1],
        'fw_id': unpacked[2],
        'fw_ver_major': unpacked[3],
        'fw_ver_minor': unpacked[4],
        'hw_id': unpacked[5],
        'hw_ver_major': unpacked[6],
        'hw_ver_minor': unpacked[7],
        'mcu_serial': unpacked[8],
        'cpu_uid': (unpacked[9], unpacked[10], unpacked[11]),
        'adc_hw_id': unpacked[12],
        'adc_ver_major': unpacked[13],
        'adc_ver_minor': unpacked[14],
        'adc_serial': unpacked[15],
        'channels_count': unpacked[16],
    }

# FW info struct (response to CMD_GET_FW_ID = 29)
FW_INFO_STRUCT = struct.Struct('<HBB I 8s 30s 48s BBH II')

def parse_fw_info(data):
    """Parse fw_info_t from ACK data of CMD_GET_FW_ID."""
    if len(data) < FW_INFO_STRUCT.size:
        return None
    unpacked = FW_INFO_STRUCT.unpack(data[:FW_INFO_STRUCT.size])
    return {
        'fw_id': unpacked[0],
        'fw_ver_major': unpacked[1],
        'fw_ver_minor': unpacked[2],
        'build_number': unpacked[3],
        'build_cfg': unpacked[4].decode('ascii').rstrip('\x00'),
        'build_time': unpacked[5].decode('ascii').rstrip('\x00'),
        'built_by': unpacked[6].decode('ascii').rstrip('\x00'),
        'variant_id': unpacked[7],
        'boot_bank': unpacked[8],
        'uptime_ms': unpacked[10],
        'reset_reason': unpacked[11],
    }

# Positional ring buffer with out-of-order packet support
class RingBuffer:
    """Numpy-backed ring buffer that inserts packets by packet_num position.
    
    Supports out-of-order arrival and uint16 wrap-around.
    """
    def __init__(self, channels: int = 2, capacity_packets: int = BUFFER_LENGTH_S * PACKET_RATE_HZ, samples_per_packet: int = SAMPLES_PER_PACKET):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.channels = channels
        self.capacity_packets = int(capacity_packets)
        self.samples_per_packet = samples_per_packet
        self.capacity_samples = self.capacity_packets * samples_per_packet

        self.lock = threading.Lock()

        # Pre-allocated storage
        self.data = np.zeros((channels, self.capacity_samples), dtype=np.int16)
        self.errors = np.zeros((channels, self.capacity_packets), dtype=np.uint8)
        self.filled = np.zeros(self.capacity_packets, dtype=bool)

        # Ring state
        self.write_base = 0       # uint16: packet_num of oldest slot in buffer
        self.base_slot = 0        # array index of write_base
        self.head = 0             # uint16: highest packet_num written
        self.count = 0            # number of filled slots
        self._initialized = False
        self.lost_packets = 0     # cumulative evicted-but-unfilled slots

    @property
    def is_empty(self) -> bool:
        return not self._initialized or self.count == 0

    def insert(self, packet_num: int, samples: list, errors: list) -> bool:
        """Insert packet data at position determined by packet_num.
        
        Args:
            packet_num: uint16 packet sequence number
            samples: list of channel data, each a sequence of samples_per_packet values
            errors: list of per-channel error bytes (one per channel)
        Returns:
            True if inserted, False if discarded (too old)
        """
        with self.lock:
            if not self._initialized:
                self.write_base = packet_num
                self.head = packet_num
                self._initialized = True

            rel = (packet_num - self.write_base) & 0xFFFF

            if rel >= 0x8000:
                # Packet is behind write_base — too old, discard
                return False

            if rel >= self.capacity_packets:
                # Packet is beyond buffer capacity — advance base
                advance = rel - self.capacity_packets + 1
                self._advance_base(advance)
                rel = (packet_num - self.write_base) & 0xFFFF

            # Update head (highest seen packet_num)
            head_diff = (packet_num - self.head) & 0xFFFF
            if 0 < head_diff < 0x8000:
                self.head = packet_num

            # Compute array slot and sample range
            slot = (self.base_slot + rel) % self.capacity_packets
            s_start = slot * self.samples_per_packet
            s_end = s_start + self.samples_per_packet

            # Write channel data
            n_ch = min(len(samples), self.channels)
            for ch in range(n_ch):
                self.data[ch, s_start:s_end] = samples[ch]

            # Write error bytes
            n_err = min(len(errors), self.channels)
            for ch in range(n_err):
                self.errors[ch, slot] = errors[ch]

            if not self.filled[slot]:
                self.filled[slot] = True
                self.count += 1

            return True

    def _advance_base(self, advance: int):
        """Advance write_base, clearing evicted slots."""
        advance = min(advance, self.capacity_packets)
        for i in range(advance):
            slot = (self.base_slot + i) % self.capacity_packets
            if self.filled[slot]:
                self.filled[slot] = False
                self.count -= 1
            else:
                self.lost_packets += 1
            # Zero evicted region
            s = slot * self.samples_per_packet
            self.data[:, s:s + self.samples_per_packet] = 0
            self.errors[:, slot] = 0
        self.base_slot = (self.base_slot + advance) % self.capacity_packets
        self.write_base = (self.write_base + advance) & 0xFFFF

    def snapshot(self):
        """Return linearized copy of buffer content for display/save.
        
        Returns None if empty, otherwise a dict with:
            'data': np.ndarray (channels, total_samples) int16
            'errors': np.ndarray (channels, total_packets) uint8
            'filled': np.ndarray (total_packets,) bool
            'total_packets': int
            'total_samples': int
            'count': int (number of filled packets)
        """
        with self.lock:
            if not self._initialized or self.count == 0:
                return None

            # Span from write_base to head (inclusive)
            total_packets = ((self.head - self.write_base) & 0xFFFF) + 1
            total_packets = min(total_packets, self.capacity_packets)
            total_samples = total_packets * self.samples_per_packet

            end_slot = (self.base_slot + total_packets) % self.capacity_packets

            if end_slot > self.base_slot:
                # Contiguous
                s_start = self.base_slot * self.samples_per_packet
                s_end = end_slot * self.samples_per_packet
                data_out = self.data[:, s_start:s_end].copy()
                err_out = self.errors[:, self.base_slot:end_slot].copy()
                filled_out = self.filled[self.base_slot:end_slot].copy()
            elif end_slot == self.base_slot and total_packets == self.capacity_packets:
                # Full buffer (exactly capacity)
                # Linearize starting from base_slot
                idx_pkt = np.arange(self.capacity_packets)
                idx_pkt = (idx_pkt + self.base_slot) % self.capacity_packets
                idx_smp = np.repeat(idx_pkt * self.samples_per_packet, self.samples_per_packet) \
                          + np.tile(np.arange(self.samples_per_packet), self.capacity_packets)
                data_out = self.data[:, idx_smp].copy()
                err_out = self.errors[:, idx_pkt].copy()
                filled_out = self.filled[idx_pkt].copy()
            else:
                # Wrapped
                s1_start = self.base_slot * self.samples_per_packet
                s2_end = end_slot * self.samples_per_packet
                data_out = np.concatenate([
                    self.data[:, s1_start:],
                    self.data[:, :s2_end]
                ], axis=1)
                err_out = np.concatenate([
                    self.errors[:, self.base_slot:],
                    self.errors[:, :end_slot]
                ], axis=1)
                filled_out = np.concatenate([
                    self.filled[self.base_slot:],
                    self.filled[:end_slot]
                ])

            return {
                'data': data_out,
                'errors': err_out,
                'filled': filled_out,
                'total_packets': total_packets,
                'total_samples': total_samples,
                'count': self.count,
            }

    def clear(self):
        """Reset buffer to empty state."""
        with self.lock:
            self.data[:] = 0
            self.errors[:] = 0
            self.filled[:] = False
            self.count = 0
            self._initialized = False
            self.write_base = 0
            self.base_slot = 0
            self.head = 0
            self.lost_packets = 0

# Async UDP socket
class AsyncSocket:
    def __init__(self, loop, local_port:int, label:str, backend: str = DEFAULT_SOCKET_BACKEND):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.loop = loop
        self.queue = asyncio.Queue()
        self._closed = False
        self._recv_task = None
        self.backend_name = 'unknown'

        socket_cls, self.backend_name = _resolve_buffered_socket_class(backend)
        self.sock = socket_cls(max_size=4096, name=label)
        self.sock.bind(port=local_port)
        self.sock.settimeout(DATA_SOCKET_RECV_TIMEOUT_S)

        self._recv_task = loop.create_task(self._recv_loop())
        self._logger.info(f'AsyncSocket backend={self.backend_name}')

    async def _recv_loop(self):
        while not self._closed:
            try:
                data, addr = self.sock.recvfrom(4096)
                self.queue.put_nowait((data, addr))
                await asyncio.sleep(0)
            except socket.timeout:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._closed:
                    break
                self._logger.warning(f'AsyncSocket recv loop exception: {e}')
                await asyncio.sleep(0.01)

    def sendto(self, data:bytes, target:Tuple[str,int]):
        self.sock.sendto(data, target)

    async def stop_receiver(self):
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            await asyncio.gather(self._recv_task, return_exceptions=True)
        self._recv_task = None

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        await self.stop_receiver()
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None
    
    def clear_queue(self):
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

# PTP mode container
class PTPMode:
    enabled: bool = True
    device_manager: DeviceManager = None
    
    def __init__(self):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.samples_awaited = 0
        self.waiting_for_trigger = False
        self.trigger_mode = False

    def fire_trigger(self, trigger_order:int|None = None):
        if not self.enabled:
            return
        if not self.trigger_mode:
            return
        if not self.waiting_for_trigger:
            return
        self.waiting_for_trigger = False
        self.device_manager.ptp_trigger(trigger_order)
        self._logger.info('PTP trigger received, starting sampling.')

ptp_mode = PTPMode()

# Single device client
class Device:
    PKT_TYPE_ACK = 0
    PKT_TYPE_ID = 1
    PKT_TYPE_DATA = 2
    PKT_TYPE_TRIGGER = 3
    PKT_TYPE_LOG  = 4
    PKT_TYPE_RESULT  = 5
    PKT_TYPE_DS_RESULT = 6
    PKT_TYPE_SAMPLE_RESULT = 7

    def __init__(self, ip:str, cmd_port:int, data_port:int, loop, samples_per_packet:int = SAMPLES_PER_PACKET):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.ip, self.cmd_port, self.data_port, self.loop = ip,cmd_port,data_port,loop
        self.channels = 2
        self.samples_per_packet = samples_per_packet
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(RECV_TIMEOUT_S)
        self.cmd_sock.connect((ip, cmd_port))
        self.buffer = RingBuffer(self.channels, samples_per_packet=samples_per_packet)
        self.id = int(ip.split('.')[3])
        self.header_struct = struct.Struct('<HH')
        self.data_struct = struct.Struct('<'+'h'*SAMPLES_PER_PACKET)
        self.silent_ping = False
        self.capture_active = False
        self.capture_counter = 0
        self.capture_limit = 0
        self.ptp_triggered = False
        self.received_last = 0
        self.input_packet_ring = deque(maxlen=PTP_TRIGGER_RING_PACKETS)
        self.first_data_order = None

    def _send_cmd(self, code:int, payload:bytes=b'', expect:bool=True, socket_ = None):
        pkt = struct.pack('<I', code) + payload
        if socket_ is None:
            self.cmd_sock.send(pkt)
        else:
            socket_.sendto(pkt, (self.ip, self.cmd_port))
        if not expect:
            return None
        try:
            return (socket_ or self.cmd_sock).recv(2048)
        except socket.timeout:
            return None
        
    def ping(self, socket_=None, silent=False)->bool:
        self.silent_ping = silent
        return bool(self._send_cmd(0, struct.pack('?', silent), expect=socket_ is None, socket_=socket_))

    def _parse_id(self, pkt:bytes | None):
        try:
            if pkt is None:
                self._logger.warning(f"Dev {self.ip} did not respond to get ID cmd.")
                return None
            pkt = _verify_crc(pkt)
            if pkt is None:
                self._logger.warning(f"Dev {self.ip} returned too short ID packet.")
                return None
            elif pkt is False:
                self._logger.warning(f"Dev {self.ip} returned ID packet with incorrect CRC.")
                return None
            info = parse_id_packet(pkt)
            self.channels = info['channels_count']
            self.buffer = RingBuffer(self.channels, samples_per_packet=self.samples_per_packet)
            self.info = info
            return info
        except Exception as e:
            self._logger.warning(f"Dev {self.ip} failed to parse ID packet: {e}")
            return None

    def get_id(self)->dict|None:
        return self._parse_id(self._send_cmd(1) or b'')

    def get_fw_id(self)->dict|None:
        """Send CMD_GET_FW_ID (29) and parse fw_info_t from ACK data."""
        pkt = self._send_cmd(29)
        if not pkt or len(pkt) < 8:  # ACK header = 8 bytes
            self._logger.warning(f"Dev {self.ip} did not respond to get FW ID cmd.")
            return None
        packet_type, error, cmd = struct.unpack('<HHI', pkt[:8])
        if packet_type != self.PKT_TYPE_ACK or cmd != 29:
            self._logger.warning(f"Dev {self.ip} unexpected response to get FW ID cmd (type={packet_type}, cmd={cmd}).")
            return None
        if error != 0:
            self._logger.warning(f"Dev {self.ip} FW ID cmd returned error {error}.")
            return None
        fw_data = pkt[8:]
        info = parse_fw_info(fw_data)
        if info is None:
            self._logger.warning(f"Dev {self.ip} FW info data too short ({len(fw_data)} bytes).")
        return info

    def set_id(self, new_id:int):
        payload = struct.pack('<B', new_id)
        return self._send_cmd(13, payload)
    
    def register_receiver(self, addr:str, port:int):
        return self._send_cmd(2, socket.inet_aton(addr)+struct.pack('<H',port))
    
    def register_logger(self, addr:str, port:int):
        return self._send_cmd(2, socket.inet_aton(addr)+struct.pack('<HB',port, 1))

    def remove_receiver(self, addr:str, port:int):
        return self._send_cmd(3, socket.inet_aton(addr)+struct.pack('<H',port))
    
    def remove_logger(self, addr:str, port:int):
        return self._send_cmd(3, socket.inet_aton(addr)+struct.pack('<HB',port, 1))
    
    def start_sampling(self, n:int=0):
        return self._send_cmd(5, struct.pack('<I',n))
    
    def start_sampling_trigger(self, n:int=0):
        return self._send_cmd(6, struct.pack('<I',n))
    
    def stop_sampling(self):
        return self._send_cmd(7)

    def startup_start(self, n: int = 0):
        """Send CMD_STARTUP_CONTROL start (sub_cmd=1) to CCU. Equivalent to system_control.py start --samples n."""
        return self._send_cmd(24, struct.pack('<BI', 1, n))

    def force_trigger(self):
        return self._send_cmd(9)
    
    def reset_counter(self):
        return self._send_cmd(10)
    
    def reset_device(self):
        return self._send_cmd(14, struct.pack('<B', 0xFE))
    
    def ptp_trigger(self, trigger_order:int|None = None):
        if not ptp_mode.enabled or not ptp_mode.trigger_mode:
            return
        self.ptp_triggered = True
        #if trigger_order is not None:
        #    self.flush_input_packet_ring(trigger_order)

    def ptp_wait_trigger(self):
        self.input_packet_ring.clear()
        return self._send_cmd(20)

    def ptp_reset(self):
        self.capture_active = False
        self.capture_counter = 0
        self.capture_limit = 0
        self.ptp_triggered = False
        self.input_packet_ring.clear()
        self.first_data_order = None
    
    def begin_capture(self, n: int):
        self.capture_active = True
        self.capture_limit = n
        self.capture_counter = 0
        self.first_data_order = None
    
    def flush_input_packet_ring(self, trigger_order:int):
        packets = [pkt for pkt in self.input_packet_ring
                if ((trigger_order - self.header_struct.unpack(pkt[:4])[1]) & 0xFFFF) < 0x8000]
        packets.sort(key=lambda pkt: (trigger_order - self.header_struct.unpack(pkt[:4])[1]) & 0xFFFF,
                    reverse=True)
        self.input_packet_ring.clear()
        for buffered_pkt in packets:
            if self.capture_counter >= self.capture_limit:
                break
            self.on_raw_packet(buffered_pkt)

    def set_clock_ctrl(self, clock_ctrl:int, save: bool = False):
        """According CLOCK_SETTINGS index."""
        param = clock_ctrl & 0xFF
        payload = struct.pack('<B', param) + struct.pack('<B', 0xAC if save else 0)
        return self._send_cmd(11, payload)
    
    def get_clock_config(self):
        pkt = self._send_cmd(15)
        if not pkt:
            self._logger.warning(f"Dev {self.ip} failed to get clock config.")
            return None
        if (l:=len(pkt)) < 10:
            self._logger.warning(f"Dev {self.ip} failed to get clock config - too short packet ({l}).")
            return None
        packet_type, error, cmd, active_config, stored_config = struct.unpack('<HHIBB', pkt[:10])
        if packet_type != self.PKT_TYPE_ACK:
            self._logger.warning(f"Dev {self.ip} failed to get clock config - unexpected packet type ({packet_type}).")
            return None
        if cmd != 15:
            self._logger.warning(f"Dev {self.ip} failed to get clock config - unexpected command ({cmd}).")
            return None
        if error != 0:
            self._logger.warning(f"Can not read stored clock config of {self.ip}, error code ({error}).")
        return (active_config, stored_config)

    def on_raw_packet(self, pkt:bytes):
        typ, order = self.header_struct.unpack(pkt[:4])
        match typ:
            case self.PKT_TYPE_ACK:
                if not self.silent_ping:
                    self._logger.info(f"Dev {self.ip} received ACK on DATA socket.")
                return
            case self.PKT_TYPE_DATA:
                if ptp_mode.enabled:
                    if not self.capture_active:
                        if ptp_mode.trigger_mode:
                            self.input_packet_ring.append(bytes(pkt))
                        return

                    if self.capture_limit > 0 and self.capture_counter >= self.capture_limit:
                        self.capture_active = False
                        return

                    self.capture_counter += 1
                    #if ptp_mode.samples_awaited <= 0:
                    #    return
                    #if ptp_mode.trigger_mode and not self.ptp_triggered:
                    #    self.input_packet_ring.append(bytes(pkt))
                    #    return
                    #if self.packet_counter >= ptp_mode.samples_awaited:
                    #    self.ptp_triggered = False
                    #self.packet_counter += 1

                data = _verify_crc(pkt)
                if data is None:
                    self._logger.warning(f"Dev {self.ip} returned too short DATA packet.")
                    self._logger.info(f"Dev {self.ip} received corrupted DATA packet.")
                    return
                elif data is False:
                    self._logger.warning(f"Dev {self.ip} returned DATA packet with incorrect CRC.")
                    return
                #print(f"[DBG] Dev {self.id} dataPacket {order} length {len(pkt)}")
                off = 12  # skip packet_type(2) + packet_num(2) + ptp_seconds(4) + ptp_nanoseconds(4)
                samples = []
                for _ in range(self.channels):
                    sig = self.data_struct.unpack(data[off:off+2*SAMPLES_PER_PACKET])
                    samples.append(sig)
                    off += 2*SAMPLES_PER_PACKET
                errs = list(data[off:off+self.channels])
                off += self.channels + (self.channels % 2)  # parity_errors + padding
                fault_state = data[off:off+2]
                self.buffer.insert(order, samples, errs)
                return order
            case self.PKT_TYPE_TRIGGER:
                # data = _verify_crc(pkt)
                # if not data:
                #     return
                #packet_num = struct.unpack('<H', data[2:4])
                #sample_num = struct.unpack('<B', data[4])
                sample_num = pkt[4]
                ptp_mode.fire_trigger(order)
                self._logger.info(f'PTP trigger received on {self.id} in packet {order} and sample {sample_num}.')
                return
            case self.PKT_TYPE_LOG:
                log_msg = pkt[4:].decode('utf-8').strip()
                self._logger.info(f"Dev {self.ip} log[{order}]: {log_msg}")
                return
            case self.PKT_TYPE_RESULT:
                if ptp_mode.enabled:
                    if not self.capture_active:
                        if ptp_mode.trigger_mode:
                            self.input_packet_ring.append(bytes(pkt))
                        return

                    if self.capture_limit > 0 and self.capture_counter >= self.capture_limit:
                        self.capture_active = False
                        return

                    self.capture_counter += 1

                data = _verify_crc(pkt)
                if data is None:
                    self._logger.warning(f"Dev {self.ip} returned too short RESULT packet.")
                    return
                elif data is False:
                    self._logger.warning(f"Dev {self.ip} returned RESULT packet with incorrect CRC.")
                    return
                samples = []
                result_code = struct.unpack('<H', data[4:6])[0]

                for bit_idx in range(self.channels):
                    bit_vals = (result_code >> bit_idx) & 1
                    samples.append([bit_vals])

                errs = []
                for e in list(data[6:10]):
                    errs.extend([e, e])

                self.buffer.insert(order, samples, errs)
                #self._logger.info(f"Dev {self.ip} packetNumber[{order}]: result {result_code}")
                return order
            case self.PKT_TYPE_ID:
                self._logger.info(f"Dev {self.ip} ID packet received on data socket.")
                self._parse_id(pkt)
                return
            case self.PKT_TYPE_DS_RESULT:
                self._logger.debug(f"Dev {self.ip} DS_RESULT packet {order} received (ignored).")
                return
            case self.PKT_TYPE_SAMPLE_RESULT:
                self._logger.debug(f"Dev {self.ip} SAMPLE_RESULT packet {order} received (ignored).")
                return

# Manager of multiple devices
class DeviceManager:
    MAX_DEVICES = 5
    def __init__(self, data_port:int = DEFAULT_DATA_PORT, socket_backend: str = DEFAULT_SOCKET_BACKEND):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.data_port = data_port
        self.socket_backend = socket_backend
        self.devices: Dict[str,Device] = {}
        self.loop = None
        self.data_socket = None
        self.dispatch_task = None
        self.loop_thread = None

    def broadcast(self, method:str, *args, **kwargs):
        for dev in self.devices.values():
            getattr(dev, method)(*args, **kwargs)

    def attach_loop(self, loop):
        self.loop = loop
        self.data_socket = AsyncSocket(loop, self.data_port, 'data', backend=self.socket_backend)

    def set_loop_thread(self, loop_thread):
        self.loop_thread = loop_thread

    def clear(self):
        for dev in self.devices.values():
            try:
                dev.cmd_sock.close()
            except Exception:
                pass
        self.devices.clear()
    
    def clear_data_queue(self):
        if self.loop and self.data_socket:
            self.loop.call_soon_threadsafe(self.data_socket.clear_queue)

    def add_device(self, ip:str, cmd_port:int = DEFAULT_CMD_PORT, samples_per_packet:int = SAMPLES_PER_PACKET):
        if len(self.devices) >= self.MAX_DEVICES or ip in self.devices:
            return
        self.devices[ip] = Device(ip, cmd_port, self.data_port, self.loop, samples_per_packet=samples_per_packet)

    def ping_all(self):
        return {ip: dev.ping() for ip,dev in self.devices.items()}
    
    def penetrate_firewall(self, silent = True):
        self.broadcast("ping", self.data_socket, silent)

    def get_all_ids(self):
        return {ip: dev.get_id() for ip,dev in self.devices.items()}

    def get_all_fw_ids(self):
        return {ip: dev.get_fw_id() for ip,dev in self.devices.items()}
    
    def register_all(self, addr:str, port:int):
        for dev in self.devices.values(): dev.register_receiver(addr, port)

    def remove_all(self, addr:str, port:int):
        for dev in self.devices.values(): dev.remove_receiver(addr, port)

    def register_logger_all(self, addr:str, port:int):
        for dev in self.devices.values(): dev.register_logger(addr, port)

    def remove_logger_all(self, addr:str, port:int):
        for dev in self.devices.values(): dev.remove_logger(addr, port)

    def get_clock_config_all(self):
        return {ip: dev.get_clock_config() for ip, dev in self.devices.items()}

    def force_trigger(self):
        #for dev in self.devices.values(): dev.force_trigger()
        self.devices[0].force_trigger()

    def ptp_trigger(self, trigger_order:int|None = None):
        for dev in self.devices.values():
            dev.ptp_trigger(trigger_order)
            dev.begin_capture(ptp_mode.samples_awaited)
    
    def ptp_wait_trigger(self):
        #for dev in self.devices.values(): dev.ptp_wait_trigger()
        self.devices[0].ptp_wait_trigger()
    
    def ptp_reset(self):
        for dev in self.devices.values(): dev.ptp_reset()
    
    def begin_capture_all(self, n: int):
        for dev in self.devices.values(): dev.begin_capture(n)

    def dispatch_loop(self, signal):
        async def run():
            try:
                while True:
                    pkt, (ip, _) = await self.data_socket.queue.get()
                    dev = self.devices.get(ip)
                    if not dev:
                        continue
                    
                    try:
                        order = dev.on_raw_packet(pkt)
                    except Exception as e:
                        self._logger.exception(f'Packet dispatch failed from {ip}: {e}')
                        continue

                    if order is not None:
                        signal.emit(ip, order)
            except asyncio.CancelledError:
                return
        self.dispatch_task = self.loop.create_task(run())

    async def _shutdown_async(self, drain_timeout: float = DATA_SOCKET_DRAIN_TIMEOUT_S):
        if self.data_socket is not None:
            await self.data_socket.stop_receiver()

        deadline = time.monotonic() + max(0.0, drain_timeout)
        while self.data_socket is not None and not self.data_socket.queue.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        if self.dispatch_task and not self.dispatch_task.done():
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
        self.dispatch_task = None

        if self.data_socket is not None:
            await self.data_socket.aclose()
            self.data_socket = None

        for dev in self.devices.values():
            try:
                dev.cmd_sock.close()
            except Exception:
                pass

    def shutdown(self, drain_timeout: float = DATA_SOCKET_DRAIN_TIMEOUT_S):
        if self.loop is not None and self.loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(drain_timeout), self.loop)
                fut.result(timeout=5)
            except Exception as e:
                self._logger.warning(f'Event loop shutdown warning: {e}')

            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass

            if self.loop_thread and self.loop_thread.is_alive() and self.loop_thread is not threading.current_thread():
                self.loop_thread.join(timeout=5)

        else:
            for dev in self.devices.values():
                try:
                    dev.cmd_sock.close()
                except Exception:
                    pass

            if self.data_socket is not None and self.data_socket.sock is not None:
                try:
                    self.data_socket.sock.close()
                except Exception:
                    pass
                self.data_socket = None

        self.loop = None
        self.devices.clear()

# Main GUI application
class Plotter(QWidget):
    # emits (ip, packet_order)
    data_ready = pyqtSignal(str, int)
    log_signal = pyqtSignal(str)

    Colors = [
        pg.mkColor(255,   0,   0), # red
        pg.mkColor(  0, 255,   0), # green
        pg.mkColor(  0,   0, 255), # blue
        pg.mkColor(255, 255,   0), # yellow
        pg.mkColor(  0, 255, 255), # azure
        pg.mkColor(255,   0, 255), # pink
        pg.mkColor(  0, 192, 192), # tyrkys
        pg.mkColor(128,   0, 255), # violet
        pg.mkColor(128, 255,   0), # limet
        pg.mkColor(  0, 255, 128), # light green
        pg.mkColor(  0, 128, 255), # light green
        pg.mkColor(255, 128,   0), # orenge
        pg.mkColor(255, 215,   0), # gold
        pg.mkColor(169,  82,  45), # brown
        pg.mkColor(255, 255, 255), # white
        pg.mkColor(192, 192, 192), # grey
        pg.mkColor(255, 255, 224), # ivory
        pg.mkColor(255, 200, 124), # light orange
        pg.mkColor(255, 128, 192), # light orange
        pg.mkColor(255, 102, 102), # salmon
        pg.mkColor(204, 153, 255), # light violet
        pg.mkColor(204, 102, 255), # lila
        pg.mkColor(102, 102, 255), # blue-violet
        pg.mkColor(  0, 128, 128), # blue-green
        pg.mkColor(128, 128,   0), # olive
        pg.mkColor(  0, 128,  64), # dark green
        pg.mkColor(  0, 128, 192), # dark tyrkys
        pg.mkColor(255, 102,   0), # dark orange
        pg.mkColor(128,   0,  32), # bordo
        pg.mkColor( 70, 130, 180), # steel blue
        pg.mkColor(210, 180, 140), # light bworn
        pg.mkColor(107, 142,  35)  # green olive
    ]

    def __init__(self, manager:DeviceManager):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self._logger.debug("Plotter GUI start")
        super().__init__()
        self.manager = manager
        self.default_cmd_port = DEFAULT_CMD_PORT
        self.last_order: Dict[str,int] = {}
        self.expected_samples = 0
        ptp_mode.device_manager = manager

        self.setWindowTitle(APPLICATION_TITLE)
        self.resize(1400, 800)

        root = QVBoxLayout(self)
        cfg = QGridLayout()
        root.addLayout(cfg)

        self.device_labels: List[QLabel] = []
        self.device_edits: List[QLineEdit] = []
        self.device_checks: List[QCheckBox] = []
        self.leader_buttons = QButtonGroup(self)
        self.leader_buttons.setExclusive(True)
        self.device_clock_settings: List[QComboBox] = []

        for i in range(DeviceManager.MAX_DEVICES):
            lb = QLabel(f'Device {i}')
            cfg.addWidget(lb, i, 0)
            self.device_labels.append(lb)

            chk = QCheckBox('Enable')
            chk.setChecked(True)
            cfg.addWidget(chk, i, 1)
            self.device_checks.append(chk)
            le = QLineEdit()
            le.setPlaceholderText('ip:port')
            if i == 0:
                le.textChanged.connect(self._update_defaults)
            cfg.addWidget(le, i, 2)
            self.device_edits.append(le)
            rb = QRadioButton('Leader')
            cfg.addWidget(rb, i, 3)
            self.leader_buttons.addButton(rb, i)

            cb_clock_settings = QComboBox()
            cb_clock_settings.addItems(CLOCK_SETTINGS)
            cb_clock_settings.setCurrentIndex(0)
            cb_clock_settings.currentTextChanged.connect(lambda text, row=i: self._update_clock_settings(row, CLOCK_SETTINGS.index(text)))
            cfg.addWidget(cb_clock_settings, i, 4)
            self.device_clock_settings.append(cb_clock_settings)

        self.leader_buttons.buttonClicked[int].connect(self._leader_changed)

        cfg.addWidget(QLabel('Receiver addr:port'), 0, 6)
        self.receiver_edit = QLineEdit(f'0.0.0.0:{DEFAULT_DATA_PORT}')
        cfg.addWidget(self.receiver_edit, 0, 7)

        cfg.addWidget(QLabel('Measurement number'), 2, 6)
        self.measurement_number_edit = QLineEdit(f'1')
        cfg.addWidget(self.measurement_number_edit, 2, 7)

        self.apply_btn = QPushButton('Apply Device List')
        cfg.addWidget(self.apply_btn, DeviceManager.MAX_DEVICES, 2)
        self.apply_btn.clicked.connect(self._apply_devices)

        #self.get_clock_config_btn = QPushButton('Get clock config')
        #cfg.addWidget(self.get_clock_config_btn, DeviceManager.MAX_DEVICES, 4)
        #self.get_clock_config_btn.clicked.connect(self._get_clock_config)

        self.save_clock_config_btn = QPushButton('Save clock config')
        cfg.addWidget(self.save_clock_config_btn, DeviceManager.MAX_DEVICES, 4)
        self.save_clock_config_btn.clicked.connect(self._save_clock_config)

        btns = QHBoxLayout()
        root.addLayout(btns)

        for label, fn in (#('Ping All'               , self._ping_all            ),
                          #('Get IDs'                , self._get_ids             ),
                          #('Register All'           , self._register_all        ),
                          #('Remove All'             , self._remove_all          ),
                          #('Register logger All'    , self._register_logger_all ),
                          #('Remove logger All'      , self._remove_logger_all   )
                          ('Apply config'            , self._apply_config        ),
                          ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        
        btns.addWidget(QLabel('Samples:'))
        self.sample_spin = QSpinBox()
        self.sample_spin.setRange(0,BUFFER_SIZE)
        self.sample_spin.setValue(10)
        btns.addWidget(self.sample_spin)

        for label, fn in (#('Start Sampling'                 , self._start_sampling                  ),
                          ('Start New Sampling'             , self._start_new_sampling              ),
                          #('Start Sampling on trigger'      , self._start_sampling_on_trigger       ),
                          ('Start New Sampling on trigger'  , self._start_new_sampling_on_trigger   ),
                          ('Save Measurement'               , self.save_measurement                 ),
                          ('Force trigger'                  , self._force_trigger                   ),
                          #('Stop Sampling'                  , self._stop_sampling                   ),
                          ('Reset Counter'                  , self._reset_counter                   ),
                          #('Clean Graf'                     , self.clear_plot                       ),
                          #('Penetrate Firewall'             , self._penetrate_firewall              ),
                          #('Save Data'                      , self.save_data                        ),
                          ('Reset devices'                   , self._reset_devices                   ),
                          ):
            b=QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)

        # Plot area with legend
        self.plot_widget = pg.GraphicsLayoutWidget()
        root.addWidget(self.plot_widget)
        self.ax = self.plot_widget.addPlot(title='Signals – device×channel')
        self.ax.showGrid(x=True,y=True,alpha=0.3)
        self.ax.setLabel('bottom','Time',units='s')
        self.ax.setLabel('left','Amplitude')
        self.ax.addLegend()
        self.curves: Dict[Tuple[str,int], pg.PlotDataItem] = {}

        # Detection plot
        self.plot_widget.nextRow()  # move to next row in the graphics layout
        self.ax_result = self.plot_widget.addPlot(title='Detection result')
        self.ax_result.showGrid(x=True, y=True, alpha=0.3)
        self.ax_result.setLabel('bottom', 'Time', units='s')
        self.ax_result.setLabel('left', 'Errors')
        self.ax_result.addLegend()
        # Link x-axis so both plots share the same time base and zoom/pan together
        self.ax_result.setXLink(self.ax)
        # curves for individual bits (bit index -> PlotDataItem)
        self.ax_result_curves: Dict[int, pg.PlotDataItem] = {}
        self.ax_result.setYRange(0, 1)

        self.error_lbl = QLabel()
        self.error_lbl.setStyleSheet('font-family: monospace')
        root.addWidget(self.error_lbl)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet('font-family: monospace; background:#f0f0f0')
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.log_output)
        root.addWidget(scroll)

        self.log_signal.connect(self.log_output.append)

        # Not needed
        #penetrator = QTimer(self)
        #penetrator.setInterval(3000)
        #penetrator.timeout.connect(self.manager.penetrate_firewall)
        #penetrator.start()

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._update_plot)
        self.timer.start()

        #self.data_ready.connect(self._check_order) # TODO: enable packet order checking

    def closeEvent(self, event):
        self.manager.shutdown()
        super().closeEvent(event)

    def _check_order(self, ip:str, order:int):
        last = self.last_order.get(ip)
        if last is not None:
            expected = (full_expected := (last + 1)) & 0xFFFF
            if order != expected:
                self._logger.warning(f'[PKT ORDER]\t{ip}: got {order:5}, expected {expected:5} ({full_expected:15})')
            next = ((last & ~0xFFFF) | order)
            if expected > 0xC000 and order <  0x4000:
                next += 0x10000
        else:
            next = order
        self.last_order[ip] = next

    def _update_defaults(self,text:str):
        try:
            if not ':' in text:
                return            
            ip, port = text.split(':')
            octs = ip.split('.')
            try:
                if not port:
                    port = self.default_cmd_port
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect((ip, port))
                my_ip = s.getsockname()[0]
                self._logger.debug(f'Detected local IP: {my_ip}')
                s.close()
            except socket.error as e:
                self._logger.debug(f'Can not detect local IP for {ip}:{port}, because of socket error "{e}", using default receiver address.')
                my_ip = None
            if len(octs) !=4 :
                return
            prefix = '.'.join(octs[:3])
            base = int(octs[3])
            port = self.default_cmd_port
            for i in range(1,DeviceManager.MAX_DEVICES):
                if self.device_checks[i].isChecked():
                    self.device_edits[i].setText(f'{prefix}.{base+i}:{port}')
            self.device_edits[0].setText(f'{prefix}.{base}:{port}')
            if my_ip is None:
                self.receiver_edit.setText(f'{prefix}.1:{DEFAULT_DATA_PORT}')
            else:
                self.receiver_edit.setText(f'{my_ip}:{DEFAULT_DATA_PORT}')
        except:
            pass

    def _apply_devices(self):
        self.manager.clear()
        self.ax.clear()
        self.curves.clear()
        count = 0
        for dev_index, (chk, le) in enumerate(zip(self.device_checks,self.device_edits)):
            if not chk.isChecked():
                continue
            txt = le.text().strip()
            if not txt:
                continue
            try:
                if ':' in txt:
                    ip, p = txt.split(':')
                    port = int(p)
                else:
                    ip = txt
                    port = self.default_cmd_port
                spp = 1 if dev_index == CCU_DEVICE_INDEX else SAMPLES_PER_PACKET
                self.manager.add_device(ip, port, samples_per_packet=spp)
                count += 1
            except:
                self._logger.warning(f'Bad entry: {txt}')
        self._logger.info(f'Applied {count} devices')

    def _ping_all(self):
        for ip,ok in self.manager.ping_all().items():
            self._logger.info(f'Ping {ip}: ' + ('OK' if ok else 'FAIL'))
            for dch, de, dl in zip(self.device_checks, self.device_edits, self.device_labels):
                if not dch.isChecked():
                    dl.setStyleSheet("")
                    continue
                dev_ip = de.text().strip().split(':')[0]
                if dev_ip == ip:
                    dl.setStyleSheet('background-color: green' if ok else 'background-color: red')

    def _get_ids(self):
        for ip,info in self.manager.get_all_ids().items():
            self._logger.info(f'ID {ip}: ' + (f"FW {info['fw_id']} v{info['fw_ver_major']}.{info['fw_ver_minor']}; channels={info['channels_count']}" if info else 'FAIL'))

    def _get_fw_ids(self):
        for ip,info in self.manager.get_all_fw_ids().items():
            if info:
                self._logger.info(f'FW_ID {ip}: {info["build_cfg"]} #{info["build_number"]} from {info["build_time"]} by {info["built_by"]}; uptime={info["uptime_ms"]}ms bank={info["boot_bank"]}')
            else:
                self._logger.warning(f'FW_ID {ip}: FAIL')

    def _register_all(self):
        try:
            addr,pr=self.receiver_edit.text().split(':')
            self.manager.register_all(addr,int(pr))
            self._logger.info(f'Registered {addr}:{pr}')
        except:
            self._logger.warning('Bad receiver address')
    
    def _register_ccu(self):
        try:
            addr,pr=self.device_edits[CCU_DEVICE_INDEX].text().split(':')
            port=DEFAULT_DATA_PORT
            self.manager.register_all(addr,port)
            self._logger.info(f'Registered {addr}:{port}')
        except:
            self._logger.warning('Bad receiver address')

    def _remove_all(self):
        try:
            addr,pr=self.receiver_edit.text().split(':')
            self.manager.remove_all(addr,int(pr))
            self._logger.info(f'Removed {addr}:{pr}')
        except:
            self._logger.warning('Bad receiver address')

    def _register_logger_all(self):
        try:
            addr,pr=self.receiver_edit.text().split(':')
            self.manager.register_logger_all(addr,int(pr))
            self._logger.info(f'Registered logger {addr}:{pr}')
        except:
            self._logger.warning('Bad logger receiver address')

    def _remove_logger_all(self):
        try:
            addr,pr=self.receiver_edit.text().split(':')
            self.manager.remove_logger_all(addr,int(pr))
            self._logger.info(f'Removed logger {addr}:{pr}')
        except:
            self._logger.warning('Bad logger receiver address')

    def _start_sampling(self):
        n = self.sample_spin.value()
        self.expected_samples = n

        if ptp_mode.enabled:
            ptp_mode.waiting_for_trigger = False
            ptp_mode.trigger_mode = False
            ptp_mode.samples_awaited = n
            self.manager.ptp_reset()
            self.manager.clear_data_queue()
            self.manager.begin_capture_all(n)
            ccu_ip = self.device_edits[CCU_DEVICE_INDEX].text().strip().split(':')[0]
            ccu_dev = self.manager.devices.get(ccu_ip)
            if ccu_dev:
                ccu_dev.startup_start(n)
                self._logger.info(f'Sent CCU startup_start (n={n}) to {ccu_ip}')
            else:
                self._logger.warning(f'CCU device not found ({ccu_ip}), startup_start not sent')
            self._logger.info(f'Started PTP sampling (n={n})')
        else:
            leader_id=self.leader_buttons.checkedId()
            leader_ip = self.device_edits[leader_id].text().strip().split(':')[0]
            for i, (ip, dev) in enumerate(self.manager.devices.items()):
                if ip != leader_ip:
                    dev.start_sampling(n)
                    self._logger.info(f'Start on follower {ip} (n={n})')
            if 0 <= leader_id < len(self.manager.devices):
                time.sleep(0.01)
                self.manager.devices[leader_ip].start_sampling(n)
                self._logger.info(f'Start on leader {leader_ip} (n={n})')
            else:
                for ip, dev in self.manager.devices.items():
                    dev.start_sampling(n)
                    self._logger.info(f'Start on {ip} (n={n})')

    def _start_sampling_on_trigger(self):
        n = self.sample_spin.value()
        self.expected_samples = n

        if ptp_mode.enabled:
            self.manager.ptp_reset()
            self.manager.clear_data_queue()
            
            ptp_mode.waiting_for_trigger = True
            ptp_mode.trigger_mode = True
            ptp_mode.samples_awaited = n
            
            self.manager.ptp_wait_trigger()
            self._logger.info(f'Wait trigger PTP sampling (n={n})')
        else:
            leader_id = self.leader_buttons.checkedId()
            leader_ip = self.device_edits[leader_id].text().strip().split(':')[0]
            for i, (ip, dev) in enumerate(self.manager.devices.items()):
                if ip != leader_ip:
                    dev.start_sampling(n)
                    self._logger.info(f'Start on follower {ip} (n={n})')
            if 0 <= leader_id < len(self.manager.devices):
                self.manager.devices[leader_ip].start_sampling_trigger(n)
                self._logger.info(f'Trigger on leader {leader_ip} (n={n})')
            else:
                for ip, dev in self.manager.devices.items():
                    dev.start_sampling(n)
                    self._logger.info(f'Start on {ip} (n={n})')

    def _start_new_sampling(self):
        self._reset_counter()
        self.clear_plot()
        QTimer(self).singleShot(100, self._start_sampling)

    def _start_new_sampling_on_trigger(self):
        self._reset_counter()
        self.clear_plot()
        QTimer(self).singleShot(100, self._start_sampling_on_trigger)

    def _stop_sampling(self):
        self.manager.broadcast('stop_sampling')
        self._logger.info('Stopped all sampling')

    def _reset_counter(self):
        self.manager.broadcast('reset_counter')
        self._logger.info('Reset counter on all devices')
        self.last_order.clear()

    def _force_trigger(self):
        #self.manager.broadcast('force_trigger')
        #self._logger.info('Force trigger on all devices')
        #leader_id = self.leader_buttons.checkedId()
        #leader_ip = self.device_edits[leader_id].text().strip().split(':')[0]
        self.manager.force_trigger()

    def _penetrate_firewall(self):
        self._logger.info('Trying to penetrate firewall')
        self.manager.penetrate_firewall(False)
    
    def _reset_devices(self):
        self._logger.info('Reset devices')
        #self.manager.broadcast('reset_device')
        for i, (ip, dev) in enumerate(self.manager.devices.items()):
            if i == 0:
                dev.reset_device()
            else:
                QTimer(self).singleShot(2000, dev.reset_device)

    def _get_clock_config(self):
        cfgs = self.manager.get_clock_config_all()
        for k in self.device_clock_settings:
            k.blockSignals(True)
        for ip, cfg in cfgs.items():
            if cfg is None:
                self._logger.warning(f"Clock config for {ip}: not available")
                continue
            active, stored = cfg
            for i, edit in enumerate(self.device_edits):
                line_ip = edit.text().strip().split(':')[0]
                if line_ip == ip:
                    self.device_clock_settings[i].setCurrentIndex(active)
                    break
            self._logger.info(f"Clock config for {ip}: active={active:02X}, stored={stored:02X}")
        for k in self.device_clock_settings:
            k.blockSignals(False)

    def _save_clock_config(self):
        reply = QMessageBox.question(self, "Save?",
                                    "Do you really want to save clock configuration to EEPROM?",
                                    QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
            
        for row in range(DeviceManager.MAX_DEVICES):
            if not self.device_checks[row].isChecked():
                continue
            ip = self.device_edits[row].text().strip().split(':')[0]
            dev = self.manager.devices.get(ip)
            if dev is not None:
                dev.set_clock_ctrl(clock_ctrl=self.device_clock_settings[row].currentIndex(), save=True)

    def clear_plot(self):
        for dev in self.manager.devices.values():
            dev.buffer.clear()
        self.ax.clear()
        self.curves.clear()

        #if hasattr(self, 'ax_result'):
        self.ax_result.clear()
        self.ax_result_curves.clear()

        self._update_plot()
        self._logger.info('Graf cleaned')
    
    def save_measurement(self):
        measurement_nr = int(self.measurement_number_edit.text())
        dir_path = 'RICE_mereni'
        if not os.path.exists(dir_path):
            os.mkdir(dir_path)
        file_name = os.path.join(dir_path, str(measurement_nr).zfill(4))
        self.save_data(file_prefix=file_name)
        measurement_nr += 1
        self.measurement_number_edit.setText(str(measurement_nr))

    def save_data(self, file_prefix=None):
        if not file_prefix:
            path, _ = QFileDialog.getSaveFileName(self, 'Save Data', '', 'CSV Files (*.csv)')
            if not path:
                return
            base = path.rstrip('.csv')
        else:
            base = file_prefix

        files = []
        for idx, (ip, dev) in enumerate(self.manager.devices.items()):
            fname = f"{base}_dev{idx}.csv"
            snap = dev.buffer.snapshot()
            with open(fname, 'w', newline='') as f:
                w = csv.writer(f)
                header = ['time'] + [f'ch{c}' for c in range(dev.channels)]
                w.writerow(header)
                if snap is not None:
                    n_samples = snap['total_samples']
                    period = SAMPLING_PERIOD if dev.samples_per_packet > 1 else PACKET_PERIOD
                    for i in range(n_samples):
                        t = i * period
                        row = [t] + [int(snap['data'][c, i]) for c in range(dev.channels)]
                        w.writerow(row)
            files.append(fname)
        fname = f"{base}.png"
        exporter = pyqtgraph.exporters.ImageExporter(self.ax)
        exporter.export(fname)
        files.append(fname)
        self._logger.info('Saved data: ' + ', '.join(files))

    def _update_plot(self):
        lines = []
        for dev_index, (ip, dev) in enumerate(self.manager.devices.items()):
            snap = dev.buffer.snapshot()
            if snap is None:
                continue

            total_packets = snap['total_packets']
            total_samples = snap['total_samples']
            data = snap['data']  # (channels, total_samples)
            err_arr = snap['errors']  # (channels, total_packets)

            if (dev_index != CCU_DEVICE_INDEX):
                # Node: x-axis in seconds from sample index
                x = np.arange(total_samples) * SAMPLING_PERIOD
                avgs = [0] * dev.channels
                for ch in range(dev.channels):
                    key = (ip, ch)
                    if key not in self.curves:
                        self.curves[key] = self.ax.plot(pen=Plotter.Colors[len(self.curves)], name=f'{ip}[{ch}]')
                    y = data[ch, :].astype(float)
                    avgs[ch] = np.mean(y[-min(len(y), SAMPLES_PER_PACKET * DEFAULT_AVG_LEN_MS):])
                    self.curves[key].setData(x[:len(y)], y)

                # Error calculation: sum of last packet's errors per channel
                errs = ','.join(str(int(err_arr[c, -1])) for c in range(dev.channels))

                # Statistics part
                received = total_packets

            # Result processing (CCU)
            else:
                received = total_packets
                if total_packets < 2:
                    avgs = [0]
                    errs = ','.join('0' for _ in range(dev.channels))
                    continue

                # Create step-interpolated x: each packet expands to SAMPLES_PER_PACKET samples
                x_pkt = np.arange(total_packets, dtype=float) * PACKET_PERIOD
                x = (
                    np.repeat(x_pkt, SAMPLES_PER_PACKET)
                    + np.tile(np.arange(SAMPLES_PER_PACKET), total_packets) * SAMPLING_PERIOD
                )

                avgs = [0]

                center = (dev.channels - 1) / 2.0
                offset_step = 0.05
                for bit_idx in range(dev.channels):
                    if bit_idx not in self.ax_result_curves:
                        color = Plotter.Colors[len(self.ax_result_curves) % len(Plotter.Colors)]
                        self.ax_result_curves[bit_idx] = self.ax_result.plot(pen=color, name=f'bit{bit_idx}')

                    y = data[bit_idx, :].astype(float)
                    y = np.repeat(y, SAMPLES_PER_PACKET)  # step interpolation
                    offset = (bit_idx - center) * offset_step
                    y_bits = y + offset

                    self.ax_result_curves[bit_idx].setData(x[:len(y_bits)], y_bits)

                # Error calculation
                errs = ','.join(str(int(err_arr[c, -1])) for c in range(dev.channels))

            # Statistics
            avgs = ', '.join(map(lambda v: f'{v:.3f}', avgs))
            sent = self.last_order.get(ip)
            if sent is None:
                sent = 0
            else:
                sent += 1
            sent = max(sent, received)
            stat_line = f'{ip}: packets = {received}/{sent}/{self.expected_samples}; errs = {errs}; avg = {avgs}'

            if (received == dev.received_last):
                if received != self.expected_samples:
                    stat_line = f'<span style="color:red;">{stat_line}</span>'
            lines.append(stat_line)
            dev.received_last = received

        self.error_lbl.setText(f'Statistic (ip: received / sent / expected packets (ms); channels parity errors; channels average per {DEFAULT_AVG_LEN_MS} ms):<br>' + 
                               "<br>".join(lines))
        self.error_lbl.setTextFormat(Qt.RichText)
    
    def _update_clock_settings(self, row: int, index: int):
        """Compose command using index as type and send a single command."""
        txt = self.device_edits[row].text().strip()
        if not txt:
            self._logger.warning(f'[ClockCtrl] Row {row}: no IP set')
            return

        ip = txt.split(':')[0]
        dev = self.manager.devices.get(ip)
        if not dev:
            self._logger.warning(f'[ClockCtrl] {ip}: device not applied yet')
            return

        clock_ctrl = self.device_clock_settings[row].currentIndex()

        dev.set_clock_ctrl(clock_ctrl=clock_ctrl, save=False)
        self._logger.info(
            f'[ClockCtrl] {ip}: type={CLOCK_SETTINGS[clock_ctrl]} ({clock_ctrl:02X}) command sent'
        )

    def _leader_changed(self, leader_id):
        for row in range(DeviceManager.MAX_DEVICES):
            if self.device_checks[row].isChecked():
                self.device_clock_settings[row].setCurrentIndex(1 if row == leader_id else 2)
    
    def _apply_config(self):
        #self._penetrate_firewall()
        #for i, f in enumerate((self._ping_all, self._register_logger_all, self._get_ids, self._get_clock_config, self._register_all, self._reset_counter)):
        # TODO: add self._leader_changed
        for i, f in enumerate((self._ping_all, self._register_logger_all, self._get_ids, self._get_fw_ids, self._get_clock_config, self._register_all, self._register_ccu, self._reset_counter)):
            QTimer(self).singleShot(i * 100, f)

def main(argv):
    with ExitStack() as stack:
        stack.enter_context(logging_:=logger.Logging())

        try:
            import default_settings as ds
        except ImportError:
            ds = None

        DEFAULT_FIRST_IP = getattr(ds, 'DEFAULT_FIRST_IP', "192.168.137.100")
        DEFAULT_LEADER = getattr(ds, 'DEFAULT_LEADER', 1)
        DEVICES_COUNT = getattr(ds, 'DEVICES_COUNT', 5)
        DEFAULT_AVG_LEN_MS = getattr(ds, 'DEFAULT_AVG_LEN_MS', 1000)
        ptp_mode.enabled = getattr(ds, 'DEFAULT_PTP_MODE_ENABLED', False)
        SOCKET_BACKEND = getattr(ds, 'SOCKET_BACKEND', DEFAULT_SOCKET_BACKEND)

        if sys.platform.startswith('win'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling,True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,True)
        app=QApplication(argv)
        manager=DeviceManager(socket_backend=SOCKET_BACKEND)
        gui=Plotter(manager)
        gui_log_handler = logger.CallbackHandler(sink_text=gui.log_signal.emit)
        gui_log_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d\t%(levelname)-8s\t%(name)-10s\t%(message)s"))
        gui_log_handler.formatter.datefmt='%H:%M:%S'
        gui_log_handler.setLevel(logging.DEBUG)
        logging_.log_printer.add_handler(gui_log_handler)
        logging_.logger.critical(f"Logging to file: {logging_.log_path}") # This has to be in console, so critical
        logging_.logger.info(f"Application started with settings: DEFAULT_FIRST_IP={DEFAULT_FIRST_IP}, DEFAULT_LEADER={DEFAULT_LEADER}, DEVICES_COUNT={DEVICES_COUNT}, DEFAULT_AVG_LEN_MS={DEFAULT_AVG_LEN_MS}, DEFAULT_PTP_MODE_ENABLED={ptp_mode.enabled}, SOCKET_BACKEND={SOCKET_BACKEND}")  
        def start_loop():
            loop=asyncio.SelectorEventLoop()
            asyncio.set_event_loop(loop)
            manager.attach_loop(loop)
            manager.dispatch_loop(gui.data_ready)
            try:
                loop.run_forever()
            finally:
                loop.close()
        loop_thread = threading.Thread(target=start_loop, daemon=False, name='udp_loop')
        manager.set_loop_thread(loop_thread)
        loop_thread.start()
        def autoinit():
            gui._update_defaults(DEFAULT_FIRST_IP + ':')
            debug = len(argv) > 1 and argv[1] == "DEBUG"
            for i, checkbox in enumerate(gui.device_checks):
                if debug:
                    checkbox.setChecked(i in (0,))
                else:
                    checkbox.setChecked(i < DEVICES_COUNT)
            gui.leader_buttons.button(DEFAULT_LEADER).setChecked(True)
            gui._apply_devices()
            gui._apply_config()
            gui.sample_spin.setValue(int(DEFAULT_AVG_LEN_MS))
        QTimer(gui).singleShot(500, autoinit)
        #gui.show()
        gui.showMaximized()
        try:
            return app.exec_()
        finally:
            manager.shutdown()

if __name__=='__main__':
    sys.exit(main(sys.argv))

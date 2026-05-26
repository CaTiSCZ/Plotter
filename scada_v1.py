# -*- coding: utf-8 -*-
"""scada.py – FDDS multi-device UDP SCADA with continuous streaming
===================================================================
Continuous streaming workflow:
  - Register as data+log receiver at all devices (CCU + nodes)
  - Data flows continuously into per-device ring buffers
  - Rolling display shows the last N seconds of data
  - Trigger packets create snapshots for review/save
  - CCU auto-detected via fw_id==0
"""
from __future__ import annotations
import logging
import logger
import struct, socket, sys, time, threading, csv, os, math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Tuple, List, Optional
from datetime import datetime
from contextlib import ExitStack

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QSpinBox, QDoubleSpinBox, QCheckBox,
    QTextEdit, QScrollArea, QFileDialog, QMessageBox, QComboBox,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView, QMainWindow,
    QAbstractItemView
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor

APPLICATION_NAME = 'Eaton FDDS SCADA'
APPLICATION_VERSION = '2.0.0'
APPLICATION_TITLE = f"{APPLICATION_NAME} v{APPLICATION_VERSION}"

# --------------- Constants ---------------
DEFAULT_CMD_PORT   = 10578
DEFAULT_DATA_PORT  = 10580
RECV_TIMEOUT_S     = 0.3
SAMPLES_PER_PACKET = 200
PACKET_RATE_HZ     = 1000
SAMPLING_PERIOD    = 1 / (SAMPLES_PER_PACKET * PACKET_RATE_HZ)
PACKET_PERIOD      = 1 / PACKET_RATE_HZ
BUFFER_LENGTH_S    = 30
BUFFER_SIZE        = int(BUFFER_LENGTH_S * SAMPLES_PER_PACKET * PACKET_RATE_HZ)
DEFAULT_AVG_LEN_MS = 1000
DEFAULT_SOCKET_BACKEND = 'auto'
DATA_SOCKET_RECV_TIMEOUT_S = 0.001  # short timeout for batch drain (1ms)
DATA_SOCKET_DRAIN_TIMEOUT_S = 0.3
SOCKET_RECV_BUFFER_SIZE = 4 * 1024 * 1024  # 4 MB OS receive buffer
CCU_FW_VERSION_ID  = 0   # fw_version.id == 0 means CCU (from version.h)
KEEPALIVE_INTERVAL_MS = 3000

# Clock config bit masks (from clock.h)
CLOCK_CONFIG_OUT_MSK    = 0x01  # bit 0
CLOCK_CONFIG_SOURCE_MSK = 0x06  # bits 2:1
CLOCK_CONFIG_FORCE_MSK  = 0x08  # bit 3
CLOCK_SOURCES = ['Internal', 'External', 'PTP HW', 'PTP SW']

# Features
FCN_QT_LOGGING = True


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


# --------------- CRC-16/CCITT ---------------
try:
    import crcmod
    _crc16_func = crcmod.predefined.mkCrcFun('crc-ccitt-false')
    def crc16_ccitt(data: bytes, poly: int = 0x1021, crc: int = 0xFFFF) -> int:
        return _crc16_func(data)
except ImportError:
    def crc16_ccitt(data: bytes, poly: int = 0x1021, crc: int = 0xFFFF) -> int:
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        return crc

CRC_STRUCT = struct.Struct('<H')

def _verify_crc(pkt: bytes) -> bytes | None:
    if not pkt or len(pkt) < 2:
        return None
    data, recv_crc = pkt[:-2], CRC_STRUCT.unpack(pkt[-2:])[0]
    return data if crc16_ccitt(data) == recv_crc else False


# --------------- ID packet parsing ---------------
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


# --------------- FW info struct ---------------
FW_INFO_STRUCT = struct.Struct('<HBB I 8s 30s 48s BBH II')

def parse_fw_info(data):
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


# --------------- RingBuffer ---------------
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
        self.data = np.zeros((channels, self.capacity_samples), dtype=np.int16)
        self.errors = np.zeros((channels, self.capacity_packets), dtype=np.uint8)
        self.filled = np.zeros(self.capacity_packets, dtype=bool)

        self.write_base = 0
        self.base_slot = 0
        self.head = 0
        self.count = 0
        self._initialized = False
        self.lost_packets = 0

    @property
    def is_empty(self) -> bool:
        return not self._initialized or self.count == 0

    def insert(self, packet_num: int, samples: list, errors: list) -> bool:
        with self.lock:
            if not self._initialized:
                self.write_base = packet_num
                self.head = packet_num
                self._initialized = True

            rel = (packet_num - self.write_base) & 0xFFFF
            if rel >= 0x8000:
                return False

            if rel >= self.capacity_packets:
                advance = rel - self.capacity_packets + 1
                self._advance_base(advance)
                rel = (packet_num - self.write_base) & 0xFFFF

            head_diff = (packet_num - self.head) & 0xFFFF
            if 0 < head_diff < 0x8000:
                self.head = packet_num

            slot = (self.base_slot + rel) % self.capacity_packets
            s_start = slot * self.samples_per_packet
            s_end = s_start + self.samples_per_packet

            n_ch = min(len(samples), self.channels)
            for ch in range(n_ch):
                self.data[ch, s_start:s_end] = samples[ch]

            n_err = min(len(errors), self.channels)
            for ch in range(n_err):
                self.errors[ch, slot] = errors[ch]

            if not self.filled[slot]:
                self.filled[slot] = True
                self.count += 1
            return True

    def _advance_base(self, advance: int):
        advance = min(advance, self.capacity_packets)
        for i in range(advance):
            slot = (self.base_slot + i) % self.capacity_packets
            if self.filled[slot]:
                self.filled[slot] = False
                self.count -= 1
            else:
                self.lost_packets += 1
            s = slot * self.samples_per_packet
            self.data[:, s:s + self.samples_per_packet] = 0
            self.errors[:, slot] = 0
        self.base_slot = (self.base_slot + advance) % self.capacity_packets
        self.write_base = (self.write_base + advance) & 0xFFFF

    def snapshot(self):
        with self.lock:
            if not self._initialized or self.count == 0:
                return None
            total_packets = ((self.head - self.write_base) & 0xFFFF) + 1
            total_packets = min(total_packets, self.capacity_packets)
            total_samples = total_packets * self.samples_per_packet
            end_slot = (self.base_slot + total_packets) % self.capacity_packets

            if end_slot > self.base_slot:
                s_start = self.base_slot * self.samples_per_packet
                s_end = end_slot * self.samples_per_packet
                data_out = self.data[:, s_start:s_end].copy()
                err_out = self.errors[:, self.base_slot:end_slot].copy()
                filled_out = self.filled[self.base_slot:end_slot].copy()
            elif end_slot == self.base_slot and total_packets == self.capacity_packets:
                idx_pkt = np.arange(self.capacity_packets)
                idx_pkt = (idx_pkt + self.base_slot) % self.capacity_packets
                idx_smp = np.repeat(idx_pkt * self.samples_per_packet, self.samples_per_packet) \
                          + np.tile(np.arange(self.samples_per_packet), self.capacity_packets)
                data_out = self.data[:, idx_smp].copy()
                err_out = self.errors[:, idx_pkt].copy()
                filled_out = self.filled[idx_pkt].copy()
            else:
                s1_start = self.base_slot * self.samples_per_packet
                s2_end = end_slot * self.samples_per_packet
                data_out = np.concatenate([self.data[:, s1_start:], self.data[:, :s2_end]], axis=1)
                err_out = np.concatenate([self.errors[:, self.base_slot:], self.errors[:, :end_slot]], axis=1)
                filled_out = np.concatenate([self.filled[self.base_slot:], self.filled[:end_slot]])

            return {
                'data': data_out,
                'errors': err_out,
                'filled': filled_out,
                'total_packets': total_packets,
                'total_samples': total_samples,
                'count': self.count,
            }

    def snapshot_range(self, from_pkt: int, to_pkt: int):
        """Return linearized copy of buffer for a specific packet range [from_pkt, to_pkt].
        Clamps to available data. Returns None if empty or no overlap.
        """
        with self.lock:
            if not self._initialized or self.count == 0:
                return None

            # Clamp from_pkt to write_base if it's behind
            from_rel = (from_pkt - self.write_base) & 0xFFFF
            if from_rel >= 0x8000:
                from_pkt = self.write_base
                from_rel = 0

            # Clamp to_pkt to head if it's ahead
            to_rel = (to_pkt - self.write_base) & 0xFFFF
            head_rel = (self.head - self.write_base) & 0xFFFF
            if to_rel > head_rel:
                to_pkt = self.head
                to_rel = head_rel

            if to_rel < from_rel:
                return None

            total_packets = to_rel - from_rel + 1
            total_samples = total_packets * self.samples_per_packet

            start_slot = (self.base_slot + from_rel) % self.capacity_packets
            end_slot = (start_slot + total_packets) % self.capacity_packets

            if total_packets == 0:
                return None

            if end_slot > start_slot:
                s_start = start_slot * self.samples_per_packet
                s_end = end_slot * self.samples_per_packet
                data_out = self.data[:, s_start:s_end].copy()
                err_out = self.errors[:, start_slot:end_slot].copy()
                filled_out = self.filled[start_slot:end_slot].copy()
            elif end_slot == start_slot and total_packets == self.capacity_packets:
                idx_pkt = (np.arange(self.capacity_packets) + start_slot) % self.capacity_packets
                idx_smp = np.repeat(idx_pkt * self.samples_per_packet, self.samples_per_packet) \
                          + np.tile(np.arange(self.samples_per_packet), self.capacity_packets)
                data_out = self.data[:, idx_smp].copy()
                err_out = self.errors[:, idx_pkt].copy()
                filled_out = self.filled[idx_pkt].copy()
            else:
                s1_start = start_slot * self.samples_per_packet
                s2_end = end_slot * self.samples_per_packet
                data_out = np.concatenate([self.data[:, s1_start:], self.data[:, :s2_end]], axis=1)
                err_out = np.concatenate([self.errors[:, start_slot:], self.errors[:, :end_slot]], axis=1)
                filled_out = np.concatenate([self.filled[start_slot:], self.filled[:end_slot]])

            return {
                'data': data_out,
                'errors': err_out,
                'filled': filled_out,
                'total_packets': total_packets,
                'total_samples': total_samples,
                'count': int(filled_out.sum()),
                'from_pkt': from_pkt,
                'to_pkt': to_pkt,
            }

    def clear(self):
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


# --------------- Async UDP socket ---------------
class AsyncSocket:
    """Thin wrapper around BufferedSocket for data reception.
    No asyncio — just provides the socket and backend info.
    """
    def __init__(self, local_port: int, label: str, backend: str = DEFAULT_SOCKET_BACKEND):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.backend_name = 'unknown'

        socket_cls, self.backend_name = _resolve_buffered_socket_class(backend)
        self.sock = socket_cls(max_size=4096, name=label)
        self.sock.bind(port=local_port)
        self.sock.settimeout(DATA_SOCKET_RECV_TIMEOUT_S)
        self._logger.info(f'AsyncSocket backend={self.backend_name}')

    def sendto(self, data: bytes, target: Tuple[str, int]):
        self.sock.sendto(data, target)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None


# --------------- Single device client ---------------
class Device:
    PKT_TYPE_ACK = 0
    PKT_TYPE_ID = 1
    PKT_TYPE_DATA = 2
    PKT_TYPE_TRIGGER = 3
    PKT_TYPE_LOG = 4
    PKT_TYPE_RESULT = 5
    PKT_TYPE_DS_RESULT = 6
    PKT_TYPE_SAMPLE_RESULT = 7

    def __init__(self, ip: str, cmd_port: int, data_port: int, loop=None, samples_per_packet: int = SAMPLES_PER_PACKET):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.ip = ip
        self.cmd_port = cmd_port
        self.data_port = data_port
        self.channels = 2
        self.samples_per_packet = samples_per_packet
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(RECV_TIMEOUT_S)
        self.cmd_sock.connect((ip, cmd_port))
        self.buffer = RingBuffer(self.channels, samples_per_packet=samples_per_packet)
        self.id = int(ip.split('.')[3])
        self.header_struct = struct.Struct('<HH')
        self.data_struct = struct.Struct('<' + 'h' * SAMPLES_PER_PACKET)
        self.silent_ping = False
        self.is_ccu = False
        self.info = None
        self.received_last = 0
        # Trigger callback — set by DeviceManager
        self._trigger_callback = None

    def _send_cmd(self, code: int, payload: bytes = b'', expect: bool = True, socket_=None):
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

    def ping(self, socket_=None, silent=False) -> bool:
        self.silent_ping = silent
        return bool(self._send_cmd(0, struct.pack('?', silent), expect=socket_ is None, socket_=socket_))

    def _parse_id(self, pkt: bytes | None):
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
            # Auto-detect CCU
            if info['fw_id'] == CCU_FW_VERSION_ID:
                self.is_ccu = True
                self.samples_per_packet = 1
            else:
                self.is_ccu = False
            self.buffer = RingBuffer(self.channels, samples_per_packet=self.samples_per_packet)
            self.info = info
            return info
        except Exception as e:
            self._logger.warning(f"Dev {self.ip} failed to parse ID packet: {e}")
            return None

    def get_id(self) -> dict | None:
        return self._parse_id(self._send_cmd(1) or b'')

    def get_fw_id(self) -> dict | None:
        pkt = self._send_cmd(29)
        if not pkt or len(pkt) < 8:
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

    def set_id(self, new_id: int):
        return self._send_cmd(13, struct.pack('<B', new_id))

    def register_receiver(self, addr: str, port: int):
        return self._send_cmd(2, socket.inet_aton(addr) + struct.pack('<H', port))

    def register_logger(self, addr: str, port: int):
        return self._send_cmd(2, socket.inet_aton(addr) + struct.pack('<HB', port, 1))

    def remove_receiver(self, addr: str, port: int):
        return self._send_cmd(3, socket.inet_aton(addr) + struct.pack('<H', port))

    def remove_logger(self, addr: str, port: int):
        return self._send_cmd(3, socket.inet_aton(addr) + struct.pack('<HB', port, 1))

    def start_sampling(self, n: int = 0):
        return self._send_cmd(5, struct.pack('<I', n))

    def start_sampling_trigger(self, n: int = 0):
        return self._send_cmd(6, struct.pack('<I', n))

    def stop_sampling(self):
        return self._send_cmd(7)

    def startup_start(self, n: int = 0):
        """CMD_STARTUP_CONTROL start (action=0)."""
        return self._send_cmd(24, struct.pack('<BI', 0, n))

    def startup_abort(self):
        """CMD_STARTUP_CONTROL abort (action=1)."""
        return self._send_cmd(24, struct.pack('<B', 1))

    def stop_system(self):
        """CMD_STOP_SYSTEM (25)."""
        return self._send_cmd(25)

    def force_trigger(self):
        return self._send_cmd(9)

    def reset_counter(self):
        return self._send_cmd(10)

    def reset_device(self):
        return self._send_cmd(14, struct.pack('<B', 0xFE))

    def reset_fault_state(self):
        return self._send_cmd(21)

    def get_system_state(self):
        return self._send_cmd(23)

    def get_net_config(self):
        pkt = self._send_cmd(30)
        if not pkt or len(pkt) < 8 + 20:
            return None
        packet_type, error, cmd = struct.unpack('<HHI', pkt[:8])
        if packet_type != self.PKT_TYPE_ACK or cmd != 30 or error != 0:
            return None
        d = pkt[8:]
        mac = d[0:6]
        ip_addr = socket.inet_ntoa(d[8:12])
        netmask = socket.inet_ntoa(d[12:16])
        gateway = socket.inet_ntoa(d[16:20])
        return {'mac': mac.hex(':'), 'ip': ip_addr, 'netmask': netmask, 'gateway': gateway}

    def get_calibration(self, source: int = 0):
        """CMD_GET_CALIBRATION (32). source: 0=RAM, 1=EEPROM."""
        return self._send_cmd(32, struct.pack('<B', source))

    def get_ptp_time(self):
        pkt = self._send_cmd(34)
        if not pkt or len(pkt) < 8 + 8:
            return None
        packet_type, error, cmd = struct.unpack('<HHI', pkt[:8])
        if packet_type != self.PKT_TYPE_ACK or cmd != 34 or error != 0:
            return None
        sec, nsec = struct.unpack('<II', pkt[8:16])
        return {'seconds': sec, 'nanoseconds': nsec}

    def enable_test_data(self):
        return self._send_cmd(18)

    def disable_test_data(self):
        return self._send_cmd(19)

    def set_clock_ctrl(self, config_byte: int, save: bool = False):
        payload = struct.pack('<BB', config_byte & 0xFF, 0xAC if save else 0)
        return self._send_cmd(11, payload)

    def get_clock_config(self):
        pkt = self._send_cmd(15)
        if not pkt:
            self._logger.warning(f"Dev {self.ip} failed to get clock config.")
            return None
        if len(pkt) < 10:
            self._logger.warning(f"Dev {self.ip} failed to get clock config - too short packet ({len(pkt)}).")
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

    def on_raw_packet(self, pkt: bytes):
        """Process a raw packet from the data socket. Returns packet order or None."""
        typ, order = self.header_struct.unpack(pkt[:4])
        match typ:
            case self.PKT_TYPE_ACK:
                if not self.silent_ping:
                    self._logger.info(f"Dev {self.ip} received ACK on DATA socket.")
                return
            case self.PKT_TYPE_DATA:
                data = _verify_crc(pkt)
                if data is None:
                    self._logger.warning(f"Dev {self.ip} returned too short DATA packet.")
                    return
                elif data is False:
                    self._logger.warning(f"Dev {self.ip} returned DATA packet with incorrect CRC.")
                    return
                off = 12  # skip packet_type(2) + packet_num(2) + ptp_seconds(4) + ptp_nanoseconds(4)
                samples = []
                for _ in range(self.channels):
                    sig = self.data_struct.unpack(data[off:off + 2 * SAMPLES_PER_PACKET])
                    samples.append(sig)
                    off += 2 * SAMPLES_PER_PACKET
                errs = list(data[off:off + self.channels])
                self.buffer.insert(order, samples, errs)
                return order
            case self.PKT_TYPE_TRIGGER:
                sample_num = pkt[4]
                self._logger.info(f'Trigger received on {self.ip} in packet {order} sample {sample_num}.')
                if self._trigger_callback:
                    self._trigger_callback(self.ip, order, sample_num)
                return
            case self.PKT_TYPE_LOG:
                log_msg = pkt[4:].decode('utf-8', errors='replace').strip()
                self._logger.info(f"Dev {self.ip} log[{order}]: {log_msg}")
                return
            case self.PKT_TYPE_RESULT:
                data = _verify_crc(pkt)
                if data is None:
                    self._logger.warning(f"Dev {self.ip} returned too short RESULT packet.")
                    return
                elif data is False:
                    self._logger.warning(f"Dev {self.ip} returned RESULT packet with incorrect CRC.")
                    return
                result_code = struct.unpack('<H', data[4:6])[0]
                samples = []
                for bit_idx in range(self.channels):
                    bit_vals = (result_code >> bit_idx) & 1
                    samples.append([bit_vals])
                errs = []
                for e in list(data[6:10]):
                    errs.extend([e, e])
                self.buffer.insert(order, samples, errs)
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


# --------------- Device Manager ---------------
class DeviceManager:
    MAX_DEVICES = 5

    def __init__(self, data_port: int = DEFAULT_DATA_PORT, socket_backend: str = DEFAULT_SOCKET_BACKEND):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.data_port = data_port
        self.socket_backend = socket_backend
        self.devices: Dict[str, Device] = {}
        self.data_socket: Optional[AsyncSocket] = None
        self._dispatch_thread: Optional[threading.Thread] = None
        self._running = False
        self.dropped_counters: Dict[str, int] = {}

    def broadcast(self, method: str, *args, **kwargs):
        for dev in self.devices.values():
            getattr(dev, method)(*args, **kwargs)

    def attach_loop(self, loop):
        # Legacy compat — no-op, we use plain thread now
        pass

    def set_loop_thread(self, loop_thread):
        # Legacy compat — no-op
        pass

    def clear(self):
        for dev in self.devices.values():
            try:
                dev.cmd_sock.close()
            except Exception:
                pass
        self.devices.clear()

    def clear_data_queue(self):
        pass  # C++ queue drains naturally

    def add_device(self, ip: str, cmd_port: int = DEFAULT_CMD_PORT, samples_per_packet: int = SAMPLES_PER_PACKET):
        if len(self.devices) >= self.MAX_DEVICES or ip in self.devices:
            return
        dev = Device(ip, cmd_port, self.data_port, None, samples_per_packet=samples_per_packet)
        self.devices[ip] = dev

    def set_trigger_callback(self, callback):
        for dev in self.devices.values():
            dev._trigger_callback = callback

    def get_ccu(self) -> Device | None:
        for dev in self.devices.values():
            if dev.is_ccu:
                return dev
        return None

    def ping_all(self):
        return {ip: dev.ping() for ip, dev in self.devices.items()}

    def penetrate_firewall(self, silent=True):
        self.broadcast("ping", self.data_socket, silent)

    def get_all_ids(self):
        return {ip: dev.get_id() for ip, dev in self.devices.items()}

    def get_all_fw_ids(self):
        return {ip: dev.get_fw_id() for ip, dev in self.devices.items()}

    def register_all(self, addr: str, port: int):
        for dev in self.devices.values():
            dev.register_receiver(addr, port)

    def remove_all(self, addr: str, port: int):
        for dev in self.devices.values():
            dev.remove_receiver(addr, port)

    def register_logger_all(self, addr: str, port: int):
        for dev in self.devices.values():
            dev.register_logger(addr, port)

    def remove_logger_all(self, addr: str, port: int):
        for dev in self.devices.values():
            dev.remove_logger(addr, port)

    def get_clock_config_all(self):
        return {ip: dev.get_clock_config() for ip, dev in self.devices.items()}

    def force_trigger(self):
        ccu = self.get_ccu()
        if ccu:
            ccu.force_trigger()
        else:
            self._logger.warning("CCU not found among registered devices — cannot send force trigger.")

    def register_at_ccu(self, addr: str, port: int):
        """Register SCADA as receiver at CCU (for result packets)."""
        ccu = self.get_ccu()
        if ccu:
            ccu.register_receiver(addr, port)
            self._logger.info(f"Registered {addr}:{port} at CCU ({ccu.ip})")
        else:
            self._logger.warning("CCU not found — skipping CCU receiver registration.")

    def start_dispatch(self, data_signal, trigger_signal):
        """Start the data socket and dispatch thread."""
        self.data_socket = AsyncSocket(self.data_port, 'data', backend=self.socket_backend)
        self._running = True
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_thread_func,
            args=(data_signal, trigger_signal),
            daemon=False,
            name='udp_dispatch'
        )
        self._dispatch_thread.start()

    def _dispatch_thread_func(self, data_signal, trigger_signal):
        """Plain thread: drain from C++ socket → process → signal GUI. No asyncio."""
        sock = self.data_socket.sock
        has_drain = hasattr(sock, 'drain')
        dropped = self.dropped_counters
        last_orders: Dict[str, int] = {}

        while self._running:
            try:
                # Get packets from the C++ buffered socket
                if has_drain:
                    batch = sock.drain(4096, 500)
                else:
                    # Fallback for Python backend
                    batch = []
                    for _ in range(500):
                        try:
                            pkt, addr = sock.recvfrom(4096)
                            batch.append((pkt, addr))
                        except socket.timeout:
                            break

                if not batch:
                    continue

                # Process all packets
                any_data = False
                for pkt, (ip, _port) in batch:
                    dev = self.devices.get(ip)
                    if not dev:
                        continue
                    try:
                        order = dev.on_raw_packet(pkt)
                    except Exception as e:
                        self._logger.exception(f'Packet dispatch failed from {ip}: {e}')
                        continue
                    if order is not None:
                        any_data = True
                        # Track order gaps
                        last = last_orders.get(ip)
                        if last is not None:
                            expected = (last + 1) & 0xFFFF
                            if order != expected:
                                missed = (order - expected) & 0xFFFF
                                if missed < 0x8000:
                                    dropped[ip] = dropped.get(ip, 0) + missed
                        last_orders[ip] = order

                # Signal GUI once per batch
                if any_data:
                    data_signal.emit('', 0)

            except Exception as e:
                if not self._running:
                    break
                self._logger.warning(f'Dispatch thread exception: {e}')
                time.sleep(0.01)

    def shutdown(self, drain_timeout: float = DATA_SOCKET_DRAIN_TIMEOUT_S):
        self._running = False
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=5)
        self._dispatch_thread = None
        if self.data_socket is not None:
            self.data_socket.close()
            self.data_socket = None
        for dev in self.devices.values():
            try:
                dev.cmd_sock.close()
            except Exception:
                pass
        self.devices.clear()


# --------------- Trigger Data Classes ---------------
@dataclass
class TriggerEvent:
    source_ips: List[str]
    packet_num: int
    sample_num: int
    arrived_at: float

@dataclass
class TriggerCapture:
    id: int
    event: TriggerEvent
    snapshots: Dict[str, dict]
    status: str  # 'pending' | 'ready' | 'saved' | 'discarded'
    save_path: Optional[str] = None


# --------------- Trigger Review Window ---------------
class TriggerReviewWindow(QMainWindow):
    def __init__(self, capture: TriggerCapture, parent=None):
        super().__init__(parent)
        self.capture = capture
        ev = capture.event
        self.setWindowTitle(f"Trigger #{capture.id} — Pkt {ev.packet_num} Sample {ev.sample_num}")
        self.resize(1200, 700)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Info label
        src_str = ', '.join(ev.source_ips)
        ts = datetime.fromtimestamp(ev.arrived_at).strftime('%H:%M:%S')
        layout.addWidget(QLabel(f"Trigger #{capture.id} | Devices: {src_str} | Pkt: {ev.packet_num} | "
                                f"Sample: {ev.sample_num} | Time: {ts}"))

        # Plots
        pw = pg.GraphicsLayoutWidget()
        layout.addWidget(pw)
        ax_analog = pw.addPlot(title='Analog data (Nodes)')
        ax_analog.showGrid(x=True, y=True, alpha=0.3)
        ax_analog.setLabel('bottom', 'Time relative to trigger', units='s')
        ax_analog.addLegend()

        pw.nextRow()
        ax_result = pw.addPlot(title='Detection result (CCU)')
        ax_result.showGrid(x=True, y=True, alpha=0.3)
        ax_result.setLabel('bottom', 'Time relative to trigger', units='s')
        ax_result.addLegend()
        ax_result.setXLink(ax_analog)

        # Plot data from snapshots
        color_idx = 0
        colors = Plotter.Colors
        trigger_pkt = ev.packet_num
        trigger_sample = ev.sample_num

        for ip, snap in capture.snapshots.items():
            if snap is None:
                continue
            from_pkt = snap['from_pkt']
            data = snap['data']
            total_samples = snap['total_samples']
            spp = data.shape[1] // snap['total_packets'] if snap['total_packets'] > 0 else 1

            # Compute trigger offset in samples
            trigger_offset = ((trigger_pkt - from_pkt) & 0xFFFF) * spp + trigger_sample

            if spp > 1:
                # Node data
                x = (np.arange(total_samples) - trigger_offset) * SAMPLING_PERIOD
                for ch in range(data.shape[0]):
                    pen = colors[color_idx % len(colors)]
                    ax_analog.plot(x, data[ch, :].astype(float), pen=pen, name=f'{ip}[{ch}]')
                    color_idx += 1
            else:
                # CCU result
                x = (np.arange(total_samples) - trigger_offset) * PACKET_PERIOD
                for ch in range(data.shape[0]):
                    pen = colors[color_idx % len(colors)]
                    y = np.repeat(data[ch, :].astype(float), SAMPLES_PER_PACKET)
                    x_expanded = (np.arange(len(y)) - trigger_offset * SAMPLES_PER_PACKET) * SAMPLING_PERIOD
                    ax_result.plot(x_expanded, y, pen=pen, name=f'{ip} bit{ch}')
                    color_idx += 1

        # Trigger marker
        for ax in (ax_analog, ax_result):
            vline = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen('r', width=2, style=Qt.DashLine))
            ax.addItem(vline)

        # Buttons
        btn_row = QHBoxLayout()
        layout.addLayout(btn_row)
        btn_save_auto = QPushButton("Save (auto)")
        btn_save_auto.clicked.connect(self._save_auto)
        btn_row.addWidget(btn_save_auto)
        btn_save_as = QPushButton("Save as...")
        btn_save_as.clicked.connect(self._save_as)
        btn_row.addWidget(btn_save_as)
        btn_discard = QPushButton("Discard")
        btn_discard.clicked.connect(self._discard)
        btn_row.addWidget(btn_discard)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)

    def _save_auto(self):
        if self.parent() and hasattr(self.parent(), '_save_trigger_capture_auto'):
            self.parent()._save_trigger_capture_auto(self.capture)

    def _save_as(self):
        if self.parent() and hasattr(self.parent(), '_save_trigger_capture_as'):
            self.parent()._save_trigger_capture_as(self.capture)

    def _discard(self):
        reply = QMessageBox.question(self, "Discard?", "Mark this trigger as discarded?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.capture.status = 'discarded'
            if self.parent() and hasattr(self.parent(), '_refresh_trigger_table'):
                self.parent()._refresh_trigger_table()


# --------------- Main GUI ---------------
class Plotter(QWidget):
    data_ready = pyqtSignal(str, int)
    trigger_signal = pyqtSignal(str, int, int)  # ip, packet_num, sample_num
    log_signal = pyqtSignal(str)

    Colors = [
        pg.mkColor(255, 0, 0), pg.mkColor(0, 255, 0), pg.mkColor(0, 0, 255),
        pg.mkColor(255, 255, 0), pg.mkColor(0, 255, 255), pg.mkColor(255, 0, 255),
        pg.mkColor(0, 192, 192), pg.mkColor(128, 0, 255), pg.mkColor(128, 255, 0),
        pg.mkColor(0, 255, 128), pg.mkColor(0, 128, 255), pg.mkColor(255, 128, 0),
        pg.mkColor(255, 215, 0), pg.mkColor(169, 82, 45), pg.mkColor(255, 255, 255),
        pg.mkColor(192, 192, 192), pg.mkColor(255, 200, 124), pg.mkColor(255, 128, 192),
        pg.mkColor(255, 102, 102), pg.mkColor(204, 153, 255), pg.mkColor(204, 102, 255),
        pg.mkColor(102, 102, 255), pg.mkColor(0, 128, 128), pg.mkColor(128, 128, 0),
    ]

    def __init__(self, manager: DeviceManager):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        super().__init__()
        self.manager = manager
        self.default_cmd_port = DEFAULT_CMD_PORT

        self.setWindowTitle(APPLICATION_TITLE)
        self.resize(1400, 900)

        root = QVBoxLayout(self)

        # ===== Device configuration grid =====
        cfg = QGridLayout()
        root.addLayout(cfg)

        self.device_labels: List[QLabel] = []
        self.device_edits: List[QLineEdit] = []
        self.device_checks: List[QCheckBox] = []
        self.device_clock_source: List[QComboBox] = []
        self.device_clock_out: List[QCheckBox] = []
        self.device_clock_force: List[QCheckBox] = []

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

            # Clock source combo
            cb_src = QComboBox()
            cb_src.addItems(CLOCK_SOURCES)
            cb_src.setCurrentIndex(0)
            cb_src.currentIndexChanged.connect(lambda idx, row=i: self._on_clock_source_changed(row, idx))
            cfg.addWidget(cb_src, i, 3)
            self.device_clock_source.append(cb_src)

            # Clock OUT checkbox
            chk_out = QCheckBox('OUT')
            chk_out.stateChanged.connect(lambda state, row=i: self._send_clock_config(row))
            cfg.addWidget(chk_out, i, 4)
            self.device_clock_out.append(chk_out)

            # Clock Force checkbox
            chk_force = QCheckBox('Force')
            chk_force.stateChanged.connect(lambda state, row=i: self._send_clock_config(row))
            cfg.addWidget(chk_force, i, 5)
            self.device_clock_force.append(chk_force)

        # Receiver address
        cfg.addWidget(QLabel('Receiver addr:port'), 0, 7)
        self.receiver_edit = QLineEdit(f'0.0.0.0:{DEFAULT_DATA_PORT}')
        cfg.addWidget(self.receiver_edit, 0, 8)

        # Measurement number
        cfg.addWidget(QLabel('Measurement #'), 1, 7)
        self.measurement_number_edit = QLineEdit('1')
        cfg.addWidget(self.measurement_number_edit, 1, 8)

        # Buttons row below device list
        self.apply_btn = QPushButton('Apply Device List')
        cfg.addWidget(self.apply_btn, DeviceManager.MAX_DEVICES, 2)
        self.apply_btn.clicked.connect(self._apply_devices)

        self.read_config_btn = QPushButton('Read Config (all)')
        cfg.addWidget(self.read_config_btn, DeviceManager.MAX_DEVICES, 3)
        self.read_config_btn.clicked.connect(self._read_all_config)

        self.save_clock_btn = QPushButton('Save Clock to EEPROM')
        cfg.addWidget(self.save_clock_btn, DeviceManager.MAX_DEVICES, 4)
        self.save_clock_btn.clicked.connect(self._save_clock_config)

        # ===== Main toolbar =====
        btns = QHBoxLayout()
        root.addLayout(btns)

        btn_apply_config = QPushButton('Apply config')
        btn_apply_config.clicked.connect(self._apply_config)
        btns.addWidget(btn_apply_config)

        btn_force_trigger = QPushButton('Force Trigger (CCU)')
        btn_force_trigger.clicked.connect(self._force_trigger)
        btns.addWidget(btn_force_trigger)

        btn_save_meas = QPushButton('Save Measurement')
        btn_save_meas.clicked.connect(self.save_measurement)
        btns.addWidget(btn_save_meas)

        btn_reset_counter = QPushButton('Reset Counter')
        btn_reset_counter.clicked.connect(self._reset_counter)
        btns.addWidget(btn_reset_counter)

        btn_reset_devices = QPushButton('Reset Devices')
        btn_reset_devices.clicked.connect(self._reset_devices)
        btns.addWidget(btn_reset_devices)

        # Display window spin
        btns.addWidget(QLabel('Display (s):'))
        self.display_spin = QDoubleSpinBox()
        self.display_spin.setRange(0.1, 30.0)
        self.display_spin.setValue(1.0)
        self.display_spin.setSingleStep(0.1)
        self.display_spin.setDecimals(1)
        btns.addWidget(self.display_spin)

        # Pre/post trigger spins
        btns.addWidget(QLabel('Pre-trigger (s):'))
        self.pre_trigger_spin = QDoubleSpinBox()
        self.pre_trigger_spin.setRange(0.0, 10.0)
        self.pre_trigger_spin.setValue(0.2)
        self.pre_trigger_spin.setSingleStep(0.1)
        self.pre_trigger_spin.setDecimals(1)
        btns.addWidget(self.pre_trigger_spin)

        btns.addWidget(QLabel('Post-trigger (s):'))
        self.post_trigger_spin = QDoubleSpinBox()
        self.post_trigger_spin.setRange(0.0, 10.0)
        self.post_trigger_spin.setValue(0.8)
        self.post_trigger_spin.setSingleStep(0.1)
        self.post_trigger_spin.setDecimals(1)
        btns.addWidget(self.post_trigger_spin)

        # Keepalive checkbox
        self.chk_keepalive = QCheckBox('Keepalive')
        self.chk_keepalive.setChecked(True)
        self.chk_keepalive.stateChanged.connect(self._on_keepalive_changed)
        btns.addWidget(self.chk_keepalive)

        # FPS label
        self.fps_label = QLabel('Plot: -- fps')
        self.fps_label.setStyleSheet('font-family: monospace')
        btns.addWidget(self.fps_label)

        # ===== Plot area =====
        self.plot_widget = pg.GraphicsLayoutWidget()
        root.addWidget(self.plot_widget)
        self.ax = self.plot_widget.addPlot(title='Signals – device×channel')
        self.ax.showGrid(x=True, y=True, alpha=0.3)
        self.ax.setLabel('bottom', 'Time', units='s')
        self.ax.setLabel('left', 'Amplitude')
        self.ax.addLegend()
        self.curves: Dict[Tuple[str, int], pg.PlotDataItem] = {}

        self.plot_widget.nextRow()
        self.ax_result = self.plot_widget.addPlot(title='Detection result')
        self.ax_result.showGrid(x=True, y=True, alpha=0.3)
        self.ax_result.setLabel('bottom', 'Time', units='s')
        self.ax_result.setLabel('left', 'Result')
        self.ax_result.addLegend()
        self.ax_result.setXLink(self.ax)
        self.ax_result.setYRange(0, 1)
        self.ax_result_curves: Dict[int, pg.PlotDataItem] = {}

        # ===== Statistics label =====
        self.error_lbl = QLabel()
        self.error_lbl.setStyleSheet('font-family: monospace')
        root.addWidget(self.error_lbl)

        # ===== Trigger table =====
        trigger_group = QGroupBox("Triggers")
        trigger_layout = QVBoxLayout(trigger_group)
        self.trigger_pending_label = QLabel("Pending: 0")
        trigger_layout.addWidget(self.trigger_pending_label)
        self.trigger_table = QTableWidget(0, 8)
        self.trigger_table.setHorizontalHeaderLabels(['#', 'Time', 'Devices', 'Pkt#', 'Sample#', 'Status', '', ''])
        self.trigger_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.trigger_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.trigger_table.setMaximumHeight(200)
        trigger_layout.addWidget(self.trigger_table)
        root.addWidget(trigger_group)

        # ===== Advanced panel =====
        self.advanced_group = QGroupBox("Advanced")
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(False)
        adv_layout = QVBoxLayout(self.advanced_group)

        adv_row1 = QHBoxLayout()
        adv_layout.addLayout(adv_row1)
        adv_row1.addWidget(QLabel('Samples:'))
        self.sample_spin = QSpinBox()
        self.sample_spin.setRange(0, BUFFER_SIZE)
        self.sample_spin.setValue(10000)
        adv_row1.addWidget(self.sample_spin)

        for label, fn in [
            ('Start Sampling', self._start_sampling),
            ('Start on Trigger', self._start_sampling_on_trigger),
            ('Stop Sampling', self._stop_sampling),
        ]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            adv_row1.addWidget(b)

        adv_row2 = QHBoxLayout()
        adv_layout.addLayout(adv_row2)
        for label, fn in [
            ('Get System State', self._get_system_state),
            ('Startup: Start', self._startup_start),
            ('Startup: Abort', self._startup_abort),
            ('Stop System', self._stop_system),
            ('Reset Fault State', self._reset_fault_state),
            ('Get Net Config', self._get_net_config),
            ('Get PTP Time', self._get_ptp_time),
            ('Enable Test Data', self._enable_test_data),
            ('Disable Test Data', self._disable_test_data),
        ]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            adv_row2.addWidget(b)

        root.addWidget(self.advanced_group)

        # ===== Log output =====
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet('font-family: monospace; background:#f0f0f0')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.log_output)
        root.addWidget(scroll)

        self.log_signal.connect(self.log_output.append)

        # ===== Timers =====
        # Adaptive plot timer (50ms check interval)
        self._plot_dirty = False
        self._last_plot_time = time.monotonic()
        self._fps_ema = 0.0

        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(50)
        self.plot_timer.timeout.connect(self._on_plot_timer)
        self.plot_timer.start()

        # Keepalive timer
        self.keepalive_timer = QTimer(self)
        self.keepalive_timer.setInterval(KEEPALIVE_INTERVAL_MS)
        self.keepalive_timer.timeout.connect(lambda: self.manager.penetrate_firewall(silent=True))

        # Trigger check timer
        self.trigger_check_timer = QTimer(self)
        self.trigger_check_timer.setInterval(200)
        self.trigger_check_timer.timeout.connect(self._check_pending_triggers)
        self.trigger_check_timer.start()

        # ===== Trigger state =====
        self._pending_triggers: List[TriggerEvent] = []
        self.trigger_captures: List[TriggerCapture] = []
        self._trigger_id_counter = 0
        self._review_windows: Dict[int, TriggerReviewWindow] = {}

        # ===== Signals =====
        self.data_ready.connect(self._on_data_ready)
        self.trigger_signal.connect(self._on_trigger_received)

    # ---------- Plot timing ----------
    def _on_data_ready(self, ip: str, order: int):
        self._plot_dirty = True

    def _on_plot_timer(self):
        if self._plot_dirty:
            now = time.monotonic()
            dt = now - self._last_plot_time
            if dt > 0:
                fps = 1.0 / dt
                self._fps_ema = 0.1 * fps + 0.9 * self._fps_ema
            self._last_plot_time = now
            self._plot_dirty = False
            self._update_plot()
            self.fps_label.setText(f'Plot: {self._fps_ema:.0f} fps')
        else:
            if time.monotonic() - self._last_plot_time > 2.0:
                self.fps_label.setText('Plot: 0 fps')



    # ---------- Trigger handling ----------
    def _on_trigger_received(self, ip: str, pkt_num: int, sample_num: int):
        # Deduplicate: if a pending trigger with |pkt_num diff| <= 2 exists, merge
        for t in self._pending_triggers:
            diff = abs(((pkt_num - t.packet_num) + 0x8000) & 0xFFFF - 0x8000)
            if diff <= 2:
                if ip not in t.source_ips:
                    t.source_ips.append(ip)
                return
        # Also check already-captured
        for c in self.trigger_captures[-20:]:
            diff = abs(((pkt_num - c.event.packet_num) + 0x8000) & 0xFFFF - 0x8000)
            if diff <= 2:
                if ip not in c.event.source_ips:
                    c.event.source_ips.append(ip)
                return

        event = TriggerEvent(source_ips=[ip], packet_num=pkt_num, sample_num=sample_num, arrived_at=time.time())
        self._pending_triggers.append(event)
        self.trigger_pending_label.setText(f"Pending: {len(self._pending_triggers)}")

    def _check_pending_triggers(self):
        if not self._pending_triggers:
            return

        post_pkts = math.ceil(self.post_trigger_spin.value() * PACKET_RATE_HZ)
        pre_pkts = math.ceil(self.pre_trigger_spin.value() * PACKET_RATE_HZ)
        timeout = self.pre_trigger_spin.value() + self.post_trigger_spin.value() + 5.0
        now = time.time()

        ready = []
        for event in self._pending_triggers[:]:
            target_pkt = (event.packet_num + post_pkts) & 0xFFFF
            all_ready = True
            timed_out = (now - event.arrived_at) > timeout

            if not timed_out:
                for dev in self.manager.devices.values():
                    if dev.buffer.is_empty:
                        all_ready = False
                        break
                    diff = (dev.buffer.head - target_pkt) & 0xFFFF
                    if diff >= 0x8000:  # head hasn't reached target yet
                        all_ready = False
                        break

            if all_ready or timed_out:
                # Create capture
                from_pkt = (event.packet_num - pre_pkts) & 0xFFFF
                to_pkt = (event.packet_num + post_pkts) & 0xFFFF
                snapshots = {}
                for ip, dev in self.manager.devices.items():
                    snapshots[ip] = dev.buffer.snapshot_range(from_pkt, to_pkt)

                self._trigger_id_counter += 1
                capture = TriggerCapture(
                    id=self._trigger_id_counter,
                    event=event,
                    snapshots=snapshots,
                    status='ready'
                )
                self.trigger_captures.append(capture)
                ready.append(event)

        for event in ready:
            self._pending_triggers.remove(event)

        if ready:
            self.trigger_pending_label.setText(f"Pending: {len(self._pending_triggers)}")
            self._refresh_trigger_table()

    def _refresh_trigger_table(self):
        self.trigger_table.setRowCount(len(self.trigger_captures))
        for row, cap in enumerate(self.trigger_captures):
            ev = cap.event
            self.trigger_table.setItem(row, 0, QTableWidgetItem(str(cap.id)))
            ts = datetime.fromtimestamp(ev.arrived_at).strftime('%H:%M:%S')
            self.trigger_table.setItem(row, 1, QTableWidgetItem(ts))
            self.trigger_table.setItem(row, 2, QTableWidgetItem(', '.join(ev.source_ips)))
            self.trigger_table.setItem(row, 3, QTableWidgetItem(str(ev.packet_num)))
            self.trigger_table.setItem(row, 4, QTableWidgetItem(str(ev.sample_num)))

            status_item = QTableWidgetItem(cap.status)
            status_colors = {'pending': QColor(180, 180, 180), 'ready': QColor(100, 150, 255),
                             'saved': QColor(100, 200, 100), 'discarded': QColor(255, 100, 100)}
            status_item.setBackground(status_colors.get(cap.status, QColor(255, 255, 255)))
            self.trigger_table.setItem(row, 5, status_item)

            # Action buttons
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(2, 2, 2, 2)

            btn_show = QPushButton("Show")
            btn_show.clicked.connect(lambda _, c=cap: self._show_trigger(c))
            btn_layout.addWidget(btn_show)

            btn_save = QPushButton("Save")
            btn_save.clicked.connect(lambda _, c=cap: self._save_trigger_capture_auto(c))
            btn_layout.addWidget(btn_save)

            btn_discard = QPushButton("Discard")
            btn_discard.clicked.connect(lambda _, c=cap: self._discard_trigger(c))
            btn_layout.addWidget(btn_discard)

            self.trigger_table.setCellWidget(row, 6, btn_widget)

            btn_remove = QPushButton("Remove")
            btn_remove.clicked.connect(lambda _, c=cap: self._remove_trigger(c))
            self.trigger_table.setCellWidget(row, 7, btn_remove)

    def _show_trigger(self, capture: TriggerCapture):
        if capture.id in self._review_windows:
            win = self._review_windows[capture.id]
            win.raise_()
            win.activateWindow()
            return
        win = TriggerReviewWindow(capture, parent=self)
        win.destroyed.connect(lambda: self._review_windows.pop(capture.id, None))
        self._review_windows[capture.id] = win
        win.show()

    def _save_trigger_capture_auto(self, capture: TriggerCapture):
        dir_path = 'RICE_mereni'
        os.makedirs(dir_path, exist_ok=True)
        base = os.path.join(dir_path, f"trigger_{capture.id:04d}")
        self._save_trigger_data(capture, base)

    def _save_trigger_capture_as(self, capture: TriggerCapture):
        path, _ = QFileDialog.getSaveFileName(self, 'Save Trigger Data', '', 'CSV Files (*.csv)')
        if not path:
            return
        base = path.rstrip('.csv')
        self._save_trigger_data(capture, base)

    def _save_trigger_data(self, capture: TriggerCapture, base: str):
        files = []
        for idx, (ip, snap) in enumerate(capture.snapshots.items()):
            fname = f"{base}_dev{idx}.csv"
            with open(fname, 'w', newline='') as f:
                w = csv.writer(f)
                if snap is not None:
                    n_ch = snap['data'].shape[0]
                    spp = snap['data'].shape[1] // snap['total_packets'] if snap['total_packets'] > 0 else 1
                    period = SAMPLING_PERIOD if spp > 1 else PACKET_PERIOD
                    header = ['time'] + [f'ch{c}' for c in range(n_ch)]
                    w.writerow(header)
                    n_samples = snap['total_samples']
                    for i in range(n_samples):
                        t = i * period
                        row = [t] + [int(snap['data'][c, i]) for c in range(n_ch)]
                        w.writerow(row)
            files.append(fname)
        capture.status = 'saved'
        capture.save_path = base
        self._logger.info(f'Trigger #{capture.id} saved: {", ".join(files)}')
        self._refresh_trigger_table()

    def _discard_trigger(self, capture: TriggerCapture):
        reply = QMessageBox.question(self, "Discard?", f"Mark trigger #{capture.id} as discarded?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            capture.status = 'discarded'
            self._refresh_trigger_table()

    def _remove_trigger(self, capture: TriggerCapture):
        if capture.status == 'ready':
            reply = QMessageBox.question(self, "Remove?",
                                         f"Trigger #{capture.id} is ready but not saved. Remove anyway?",
                                         QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        self.trigger_captures.remove(capture)
        self._review_windows.pop(capture.id, None)
        self._refresh_trigger_table()

    # ---------- Clock config ----------
    def _build_clock_config_byte(self, row: int) -> int:
        source = self.device_clock_source[row].currentIndex()
        out = 1 if self.device_clock_out[row].isChecked() else 0
        force = 1 if self.device_clock_force[row].isChecked() else 0
        return (source << 1) | out | (force << 3)

    def _decompose_clock_config_byte(self, config: int) -> Tuple[int, bool, bool]:
        source = (config & CLOCK_CONFIG_SOURCE_MSK) >> 1
        out = bool(config & CLOCK_CONFIG_OUT_MSK)
        force = bool(config & CLOCK_CONFIG_FORCE_MSK)
        return source, out, force

    def _on_clock_source_changed(self, row: int, idx: int):
        # Disable Force for External (source=1)
        self.device_clock_force[row].setEnabled(idx != 1)
        if idx == 1:
            self.device_clock_force[row].setChecked(False)
        self._send_clock_config(row)

    def _send_clock_config(self, row: int):
        ip = self.device_edits[row].text().strip().split(':')[0]
        dev = self.manager.devices.get(ip)
        if not dev:
            return
        config_byte = self._build_clock_config_byte(row)
        dev.set_clock_ctrl(config_byte, save=False)
        self._logger.info(f'[ClockCtrl] {ip}: config=0x{config_byte:02X} sent')

    def _read_all_config(self):
        cfgs = self.manager.get_clock_config_all()
        for ip, cfg in cfgs.items():
            if cfg is None:
                self._logger.warning(f"Clock config for {ip}: not available")
                continue
            active, stored = cfg
            src_a, out_a, force_a = self._decompose_clock_config_byte(active)
            src_s, out_s, force_s = self._decompose_clock_config_byte(stored)
            # Update UI
            for i, edit in enumerate(self.device_edits):
                line_ip = edit.text().strip().split(':')[0]
                if line_ip == ip:
                    self.device_clock_source[i].blockSignals(True)
                    self.device_clock_out[i].blockSignals(True)
                    self.device_clock_force[i].blockSignals(True)
                    self.device_clock_source[i].setCurrentIndex(min(src_a, 3))
                    self.device_clock_out[i].setChecked(out_a)
                    self.device_clock_force[i].setChecked(force_a)
                    self.device_clock_force[i].setEnabled(src_a != 1)
                    self.device_clock_source[i].blockSignals(False)
                    self.device_clock_out[i].blockSignals(False)
                    self.device_clock_force[i].blockSignals(False)
                    break
            src_name_a = CLOCK_SOURCES[src_a] if src_a < len(CLOCK_SOURCES) else f'?{src_a}'
            src_name_s = CLOCK_SOURCES[src_s] if src_s < len(CLOCK_SOURCES) else f'?{src_s}'
            self._logger.info(f"Clock {ip}: active={src_name_a} OUT={'Y' if out_a else 'N'} Force={'Y' if force_a else 'N'} (0x{active:02X})"
                              f" | stored={src_name_s} OUT={'Y' if out_s else 'N'} Force={'Y' if force_s else 'N'} (0x{stored:02X})")

        # Also read net config for all devices
        for ip, dev in self.manager.devices.items():
            net = dev.get_net_config()
            if net:
                self._logger.info(f"Net {ip}: IP={net['ip']} Mask={net['netmask']} GW={net['gateway']} MAC={net['mac']}")

    def _save_clock_config(self):
        reply = QMessageBox.question(self, "Save?",
                                     "Save clock configuration to EEPROM on all devices?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for row in range(DeviceManager.MAX_DEVICES):
            if not self.device_checks[row].isChecked():
                continue
            ip = self.device_edits[row].text().strip().split(':')[0]
            dev = self.manager.devices.get(ip)
            if dev is not None:
                config_byte = self._build_clock_config_byte(row)
                dev.set_clock_ctrl(config_byte, save=True)
                self._logger.info(f'[ClockCtrl] {ip}: config=0x{config_byte:02X} saved to EEPROM')

    # ---------- Keepalive ----------
    def _on_keepalive_changed(self, state):
        if state == Qt.Checked:
            self.keepalive_timer.start()
        else:
            self.keepalive_timer.stop()

    # ---------- Device setup ----------
    def _update_defaults(self, text: str):
        try:
            if ':' not in text:
                return
            ip, port = text.split(':')
            octs = ip.split('.')
            try:
                if not port:
                    port = self.default_cmd_port
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect((ip, int(port) if port else self.default_cmd_port))
                my_ip = s.getsockname()[0]
                s.close()
            except socket.error:
                my_ip = None
            if len(octs) != 4:
                return
            prefix = '.'.join(octs[:3])
            base = int(octs[3])
            cmd_port = self.default_cmd_port
            for i in range(1, DeviceManager.MAX_DEVICES):
                if self.device_checks[i].isChecked():
                    self.device_edits[i].setText(f'{prefix}.{base + i}:{cmd_port}')
            self.device_edits[0].setText(f'{prefix}.{base}:{cmd_port}')
            if my_ip is None:
                self.receiver_edit.setText(f'{prefix}.1:{DEFAULT_DATA_PORT}')
            else:
                self.receiver_edit.setText(f'{my_ip}:{DEFAULT_DATA_PORT}')
        except Exception:
            pass

    def _apply_devices(self):
        self.manager.clear()
        self.ax.clear()
        self.curves.clear()
        self.ax_result.clear()
        self.ax_result_curves.clear()
        count = 0
        for chk, le in zip(self.device_checks, self.device_edits):
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
                self.manager.add_device(ip, port)
                count += 1
            except Exception:
                self._logger.warning(f'Bad entry: {txt}')
        # Set trigger callback
        self.manager.set_trigger_callback(
            lambda ip, pkt, smp: self.trigger_signal.emit(ip, pkt, smp)
        )
        self._logger.info(f'Applied {count} devices')

    # ---------- Config sequence ----------
    def _apply_config(self):
        sequence = [
            self._ping_all,
            self._register_logger_all,
            self._get_ids,
            self._get_fw_ids,
            self._read_all_config,
            self._register_all,
            self._register_ccu,
            self._reset_counter,
        ]
        for i, f in enumerate(sequence):
            QTimer(self).singleShot(i * 100, f)

    def _ping_all(self):
        for ip, ok in self.manager.ping_all().items():
            self._logger.info(f'Ping {ip}: ' + ('OK' if ok else 'FAIL'))
            for dch, de, dl in zip(self.device_checks, self.device_edits, self.device_labels):
                if not dch.isChecked():
                    dl.setStyleSheet("")
                    continue
                dev_ip = de.text().strip().split(':')[0]
                if dev_ip == ip:
                    dl.setStyleSheet('background-color: green' if ok else 'background-color: red')

    def _get_ids(self):
        for ip, info in self.manager.get_all_ids().items():
            if info:
                role = "CCU" if info['fw_id'] == CCU_FW_VERSION_ID else "Node"
                self._logger.info(f'ID {ip} ({role}): FW {info["fw_id"]} v{info["fw_ver_major"]}.{info["fw_ver_minor"]}; channels={info["channels_count"]}')
            else:
                self._logger.warning(f'ID {ip}: FAIL')
        # Check CCU presence
        if not self.manager.get_ccu():
            self._logger.warning("CCU not found among devices (no device with fw_id==0). System will work without CCU for standalone operation.")

    def _get_fw_ids(self):
        for ip, info in self.manager.get_all_fw_ids().items():
            if info:
                self._logger.info(f'FW_ID {ip}: {info["build_cfg"]} #{info["build_number"]} from {info["build_time"]} by {info["built_by"]}; uptime={info["uptime_ms"]}ms bank={info["boot_bank"]}')
            else:
                self._logger.warning(f'FW_ID {ip}: FAIL')

    def _register_all(self):
        try:
            addr, pr = self.receiver_edit.text().split(':')
            self.manager.register_all(addr, int(pr))
            self._logger.info(f'Registered data receiver {addr}:{pr} at all devices')
        except Exception:
            self._logger.warning('Bad receiver address')

    def _register_ccu(self):
        """Register SCADA as receiver at CCU for result packets."""
        try:
            addr, pr = self.receiver_edit.text().split(':')
            self.manager.register_at_ccu(addr, int(pr))
        except Exception:
            self._logger.warning('Bad receiver address for CCU registration')

    def _register_logger_all(self):
        try:
            addr, pr = self.receiver_edit.text().split(':')
            self.manager.register_logger_all(addr, int(pr))
            self._logger.info(f'Registered logger {addr}:{pr} at all devices')
        except Exception:
            self._logger.warning('Bad logger receiver address')

    # ---------- Main actions ----------
    def _force_trigger(self):
        self.manager.force_trigger()
        self._logger.info('Force trigger sent to CCU')

    def _reset_counter(self):
        self.manager.broadcast('reset_counter')
        self._logger.info('Reset counter on all devices')
        self.manager.dropped_counters.clear()

    def _reset_devices(self):
        self._logger.info('Reset devices')
        for i, (ip, dev) in enumerate(self.manager.devices.items()):
            if i == 0:
                dev.reset_device()
            else:
                QTimer(self).singleShot(2000, dev.reset_device)

    # ---------- Advanced commands ----------
    def _start_sampling(self):
        n = self.sample_spin.value()
        ccu = self.manager.get_ccu()
        if ccu:
            ccu.startup_start(n)
            self._logger.info(f'Sent startup_start (n={n}) to CCU')
        else:
            self.manager.broadcast('start_sampling', n)
            self._logger.info(f'Start sampling (n={n}) broadcast to all')

    def _start_sampling_on_trigger(self):
        n = self.sample_spin.value()
        self.manager.broadcast('start_sampling_trigger', n)
        self._logger.info(f'Start sampling on trigger (n={n}) broadcast')

    def _stop_sampling(self):
        self.manager.broadcast('stop_sampling')
        self._logger.info('Stopped all sampling')

    def _get_system_state(self):
        ccu = self.manager.get_ccu()
        if ccu:
            pkt = ccu.get_system_state()
            if pkt:
                self._logger.info(f'System state (raw): {pkt.hex()}')
            else:
                self._logger.warning('Get system state: no response')
        else:
            self._logger.warning('CCU not available')

    def _startup_start(self):
        n = self.sample_spin.value()
        ccu = self.manager.get_ccu()
        if ccu:
            ccu.startup_start(n)
            self._logger.info(f'Startup start (n={n}) sent to CCU')
        else:
            self._logger.warning('CCU not available')

    def _startup_abort(self):
        ccu = self.manager.get_ccu()
        if ccu:
            ccu.startup_abort()
            self._logger.info('Startup abort sent to CCU')
        else:
            self._logger.warning('CCU not available')

    def _stop_system(self):
        ccu = self.manager.get_ccu()
        if ccu:
            ccu.stop_system()
            self._logger.info('Stop system sent to CCU')
        else:
            self._logger.warning('CCU not available')

    def _reset_fault_state(self):
        self.manager.broadcast('reset_fault_state')
        self._logger.info('Reset fault state on all devices')

    def _get_net_config(self):
        for ip, dev in self.manager.devices.items():
            net = dev.get_net_config()
            if net:
                self._logger.info(f'Net {ip}: IP={net["ip"]} Mask={net["netmask"]} GW={net["gateway"]} MAC={net["mac"]}')
            else:
                self._logger.warning(f'Net config {ip}: FAIL')

    def _get_ptp_time(self):
        for ip, dev in self.manager.devices.items():
            t = dev.get_ptp_time()
            if t:
                self._logger.info(f'PTP time {ip}: {t["seconds"]}.{t["nanoseconds"]:09d}')
            else:
                self._logger.warning(f'PTP time {ip}: FAIL')

    def _enable_test_data(self):
        self.manager.broadcast('enable_test_data')
        self._logger.info('Enable test data on all')

    def _disable_test_data(self):
        self.manager.broadcast('disable_test_data')
        self._logger.info('Disable test data on all')

    # ---------- Data save ----------
    def save_measurement(self):
        measurement_nr = int(self.measurement_number_edit.text())
        dir_path = 'RICE_mereni'
        os.makedirs(dir_path, exist_ok=True)
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

    def clear_plot(self):
        for dev in self.manager.devices.values():
            dev.buffer.clear()
        self.ax.clear()
        self.curves.clear()
        self.ax_result.clear()
        self.ax_result_curves.clear()
        self._update_plot()
        self._logger.info('Plot cleared')

    # ---------- Plot update ----------
    def _update_plot(self):
        display_s = self.display_spin.value()
        n_display_samples = int(display_s * SAMPLES_PER_PACKET * PACKET_RATE_HZ)
        n_display_pkts = int(display_s * PACKET_RATE_HZ)

        lines = []
        for ip, dev in self.manager.devices.items():
            snap = dev.buffer.snapshot()
            if snap is None:
                continue

            total_packets = snap['total_packets']
            total_samples = snap['total_samples']
            data = snap['data']
            err_arr = snap['errors']

            if not dev.is_ccu:
                # Node: analog data
                show_samples = min(n_display_samples, total_samples)
                x = np.arange(show_samples) * SAMPLING_PERIOD
                data_slice = data[:, -show_samples:] if total_samples > show_samples else data

                avgs = [0] * dev.channels
                for ch in range(dev.channels):
                    key = (ip, ch)
                    if key not in self.curves:
                        self.curves[key] = self.ax.plot(pen=Plotter.Colors[len(self.curves) % len(Plotter.Colors)], name=f'{ip}[{ch}]')
                    y = data_slice[ch, :].astype(float)
                    avgs[ch] = np.mean(y[-min(len(y), SAMPLES_PER_PACKET * DEFAULT_AVG_LEN_MS):]) if len(y) > 0 else 0
                    self.curves[key].setData(x[:len(y)], y)

                errs = ','.join(str(int(err_arr[c, -1])) for c in range(dev.channels)) if total_packets > 0 else '0'
                received = total_packets
            else:
                # CCU: result data
                show_pkts = min(n_display_pkts, total_packets)
                if show_pkts < 2:
                    continue

                x_pkt = np.arange(show_pkts, dtype=float) * PACKET_PERIOD
                x = (np.repeat(x_pkt, SAMPLES_PER_PACKET)
                     + np.tile(np.arange(SAMPLES_PER_PACKET), show_pkts) * SAMPLING_PERIOD)

                data_slice = data[:, -show_pkts:] if total_packets > show_pkts else data

                center = (dev.channels - 1) / 2.0
                offset_step = 0.05
                for bit_idx in range(dev.channels):
                    if bit_idx not in self.ax_result_curves:
                        color = Plotter.Colors[len(self.ax_result_curves) % len(Plotter.Colors)]
                        self.ax_result_curves[bit_idx] = self.ax_result.plot(pen=color, name=f'bit{bit_idx}')
                    y = data_slice[bit_idx, :].astype(float)
                    y = np.repeat(y, SAMPLES_PER_PACKET)
                    offset = (bit_idx - center) * offset_step
                    y_bits = y + offset
                    self.ax_result_curves[bit_idx].setData(x[:len(y_bits)], y_bits)

                errs = ','.join(str(int(err_arr[c, -1])) for c in range(dev.channels)) if total_packets > 0 else '0'
                avgs = [0]
                received = total_packets

            # Statistics
            role = " (CCU)" if dev.is_ccu else ""
            avgs_str = ', '.join(f'{v:.1f}' for v in avgs)
            dropped = self.manager.dropped_counters.get(ip, 0)
            stat_line = f'{ip}{role}: pkts={received}; dropped={dropped}; errs={errs}; avg={avgs_str}'

            if received == dev.received_last and received > 0:
                stat_line = f'<span style="color:red;">{stat_line}</span>'
            lines.append(stat_line)
            dev.received_last = received

        self.error_lbl.setText('<br>'.join(lines))
        self.error_lbl.setTextFormat(Qt.RichText)

    # ---------- Lifecycle ----------
    def closeEvent(self, event):
        self.manager.shutdown()
        super().closeEvent(event)


# --------------- Main ---------------
def main(argv):
    with ExitStack() as stack:
        stack.enter_context(logging_ := logger.Logging())

        try:
            import default_settings as ds
        except ImportError:
            ds = None

        DEFAULT_FIRST_IP = getattr(ds, 'DEFAULT_FIRST_IP', "192.168.137.100")
        DEVICES_COUNT = getattr(ds, 'DEVICES_COUNT', 5)
        DEFAULT_AVG_LEN_MS_SETTING = getattr(ds, 'DEFAULT_AVG_LEN_MS', 1000)
        SOCKET_BACKEND = getattr(ds, 'SOCKET_BACKEND', DEFAULT_SOCKET_BACKEND)
        PRE_TRIGGER_S = getattr(ds, 'DEFAULT_PRE_TRIGGER_S', 0.2)
        POST_TRIGGER_S = getattr(ds, 'DEFAULT_POST_TRIGGER_S', 0.8)
        DISPLAY_WINDOW_S = getattr(ds, 'DEFAULT_DISPLAY_WINDOW_S', 1.0)
        PENETRATE_FIREWALL = getattr(ds, 'DEFAULT_PENETRATE_FIREWALL', True)

        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        app = QApplication(argv)
        manager = DeviceManager(socket_backend=SOCKET_BACKEND)
        gui = Plotter(manager)

        # Connect log handler
        gui_log_handler = logger.CallbackHandler(sink_text=gui.log_signal.emit)
        gui_log_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d\t%(levelname)-8s\t%(name)-10s\t%(message)s"))
        gui_log_handler.formatter.datefmt = '%H:%M:%S'
        gui_log_handler.setLevel(logging.DEBUG)
        logging_.log_printer.add_handler(gui_log_handler)
        logging_.logger.critical(f"Logging to file: {logging_.log_path}")
        logging_.logger.info(f"Application started: DEFAULT_FIRST_IP={DEFAULT_FIRST_IP}, DEVICES_COUNT={DEVICES_COUNT}, SOCKET_BACKEND={SOCKET_BACKEND}")

        def start_dispatch():
            manager.start_dispatch(gui.data_ready, gui.trigger_signal)

        QTimer(gui).singleShot(100, start_dispatch)

        def autoinit():
            gui._update_defaults(DEFAULT_FIRST_IP + ':')
            for i, checkbox in enumerate(gui.device_checks):
                checkbox.setChecked(i < DEVICES_COUNT)
            gui._apply_devices()
            # Apply settings from defaults
            gui.display_spin.setValue(DISPLAY_WINDOW_S)
            gui.pre_trigger_spin.setValue(PRE_TRIGGER_S)
            gui.post_trigger_spin.setValue(POST_TRIGGER_S)
            gui.chk_keepalive.setChecked(PENETRATE_FIREWALL)
            if PENETRATE_FIREWALL:
                gui.keepalive_timer.start()
            gui._apply_config()

        QTimer(gui).singleShot(500, autoinit)
        gui.showMaximized()
        try:
            return app.exec_()
        finally:
            manager.shutdown()


if __name__ == '__main__':
    sys.exit(main(sys.argv))

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
os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PyQt5') # ensure PyQt5 is used for pyqtgraph in case of PyQt6 also being installed
import pyqtgraph as pg
import pyqtgraph.exporters
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QSpinBox, QCheckBox, QTextEdit,
    QScrollArea, QRadioButton, QButtonGroup, QFileDialog, QMessageBox, QComboBox
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

# Shared FDDS protocol core (vendored copy of the firmware repo's utils/fdds).
# Single source of truth for packet/command enums, struct layouts and CRC so the
# protocol stays in sync with the firmware. Re-copy fdds/ from the FW repo when
# the wire protocol changes.
from fdds.protocol import (
    PACKET, CMD, STRUCT,
    SAMPLES_PER_PACKET as FDDS_SAMPLES_PER_PACKET,
    GATHERING_DEVICES as FDDS_GATHERING_DEVICES,
    ACQUISITION_CHANNELS as FDDS_ACQUISITION_CHANNELS,
    MAX_RECEIVERS as FDDS_MAX_RECEIVERS,
    RECEIVER_TYPE_DATA, RECEIVER_TYPE_LOG, RECEIVER_TYPE_DBG,
    UDP_CMD_PORT, UDP_DATA_PORT,
)
from fdds.crc import crc16_ccitt

APPLICATION_NAME = 'Eaton FDDS SCADA'
APPLICATION_VERSION = '1.12.0'
APPLICATION_TITLE = f"{APPLICATION_NAME} v{APPLICATION_VERSION}"

# Constants (protocol-level values sourced from the shared fdds core)
DEFAULT_CMD_PORT   = UDP_CMD_PORT
DEFAULT_DATA_PORT  = UDP_DATA_PORT
RECV_TIMEOUT_S     = 0.3
SAMPLES_PER_PACKET = FDDS_SAMPLES_PER_PACKET
PACKET_RATE_HZ     = 1000
SAMPLING_PERIOD    = 1/(SAMPLES_PER_PACKET*PACKET_RATE_HZ)
PACKET_PERIOD      = 1/(PACKET_RATE_HZ)
NS_PER_SAMPLE      = round(SAMPLING_PERIOD*1e9)  # per-sample PTP step in nanoseconds (5 us)
GATHERING_DEVICES  = FDDS_GATHERING_DEVICES   # CCU result packet: number of nodes the CCU gathers from
ACQUISITION_CHANNELS = FDDS_ACQUISITION_CHANNELS # CCU result packet: ADC channels per node
# Per-device sample-buffer length (max record length). Backed by preallocated numpy
# ring buffers (np.empty -> OS commits resident pages lazily), so a larger value
# only raises the worst-case footprint, not the steady-state RAM of short records.
# Overridable via default_settings.py.
BUFFER_LENGTH_S    = 30
BUFFER_SIZE        = int(BUFFER_LENGTH_S*SAMPLES_PER_PACKET*PACKET_RATE_HZ)
MAX_CAPTURE_PACKETS = BUFFER_SIZE // SAMPLES_PER_PACKET  # max pre+post packets that fit the sample buffer
DEFAULT_AVG_LEN_MS = 1000 # could be overwritten by default_settings.py
CCU_DEVICE_INDEX   = 0
DEFAULT_SOCKET_BACKEND = 'auto'
DATA_SOCKET_RECV_TIMEOUT_S = 0.02
DATA_SOCKET_DRAIN_TIMEOUT_S = 0.3
# OS UDP receive buffer (SO_RCVBUF) for the data socket. The default Windows value
# (~64 KB, ~80 packets) overflows during the synchronous trigger flush burst
# (pretrigger+post packets x devices re-parsed in one go), dropping the odd packet
# on a single device. A large buffer absorbs the burst. Overridable via default_settings.py.
DATA_SOCKET_RCVBUF_BYTES = 16 * 1024 * 1024
PTP_TRIGGER_RING_PACKETS = 500  # default pre-trigger ring size; overridable via default_settings.py
# Extra post-trigger packets captured beyond the requested window. The capture is
# bounded by a packet COUNT, but UDP reordering (a pre-trigger packet arriving after
# the trigger), duplicates or bad-CRC packets each consume a capture slot without
# landing in the window, which would otherwise truncate the tail (last window packet
# never stored -> 199/200). This margin absorbs such strays; the plot/CSV trim back
# to exactly samples_awaited so the extra packets are harmless. Overridable via default_settings.py.
TRIGGER_CAPTURE_MARGIN_PACKETS = 16
# Firewall penetration mode for the stall diagnostic (overridable via default_settings.py):
#   'off'     - never attempt firewall penetration; treat the stall as genuine.
#   'on'      - send a silent ping through the data socket (default).
#   'verbose' - same, but the ping is not silent so it shows up in the device log.
FIREWALL_PENETRATION_MODES = ('off', 'on', 'verbose')
FIREWALL_PENETRATION = 'on'

# System startup control (command codes sourced from the shared fdds CMD enum)
CMD_GET_RECEIVERS         = int(CMD.GET_RECEIVERS)
CMD_GET_ACQUISITION_STATE = int(CMD.GET_ACQUISITION_STATE)
CMD_GET_SYSTEM_STATE      = int(CMD.GET_SYSTEM_STATE)
CMD_STARTUP_CONTROL       = int(CMD.STARTUP_CONTROL)
CMD_STOP_SYSTEM           = int(CMD.STOP_SYSTEM)
MAX_RECEIVERS = FDDS_MAX_RECEIVERS
SYSTEM_STATE_IDLE    = 0
SYSTEM_STATE_RUNNING = 9
SYSTEM_STATE_FAILED  = 10
SYSTEM_STARTUP_STATE_NAMES = {
    0: 'IDLE', 1: 'PINGING', 2: 'REGISTERING', 3: 'GETTING_IDS', 4: 'VERIFYING_CLOCKS',
    5: 'WAITING_SYNC', 6: 'RESETTING_COUNTERS', 7: 'STARTING_GATHERING',
    8: 'STARTING_MEASUREMENT', 9: 'RUNNING', 10: 'FAILED',
}
SYSTEM_STARTUP_ERROR_NAMES = {
    0: 'NONE', 1: 'NO_NODES', 2: 'PING_TIMEOUT', 3: 'REGISTER_FAILED', 4: 'GET_ID_FAILED',
    5: 'CLOCK_MISMATCH', 6: 'CLOCK_FREQ', 7: 'SYNC_TIMEOUT', 8: 'COUNTER_RESET_FAILED',
    9: 'GATHERING_FAILED', 10: 'START_MEAS_FAILED', 11: 'ABORTED',
}
SYSTEM_ERROR_CLOCK_FREQ = 6
SYSTEM_CLOCK_FREQ_MIN = 396000
SYSTEM_CLOCK_FREQ_MAX = 404000
SYSTEM_STATUS_POLL_INTERVAL_MS = 1000
SYSTEM_STATUS_POLL_MAX = 120          # ~2 min, like the FW CLI
SYSTEM_WATCHDOG_INTERVAL_MS = 1000
SYSTEM_DATA_STALL_TIMEOUT_S = 2.0     # no data for this long => read state by command
SYSTEM_DIAG_WAIT_MS = 600             # wait after a diagnostic step to see if data resumes
# System status label colours (matches the green/red device-label scheme)
SYSTEM_STATUS_COLOR_OK    = 'green'     # RUNNING
SYSTEM_STATUS_COLOR_ERROR = 'red'       # FAILED / no CCU / no ACK / data stalled / timeout
SYSTEM_STATUS_COLOR_BUSY  = '#ffb84d'   # startup in progress / starting / no response (transient)
SYSTEM_STATUS_COLOR_IDLE  = '#cfcfcf'   # IDLE / stopped (neutral)

# Brief blue flash on the device label when a trigger packet arrives from it
TRIGGER_FLASH_MS = 250
TRIGGER_FLASH_STYLE = 'background-color: #2196f3; color: white'

# Features
FCN_QT_LOGGING = True  # Enable Qt logging handler

CLOCK_FORCED = ['', 'Forced ']
CLOCK_SOURCES = ['Internal', 'External', 'PTP HW', 'PTP SW']
CLOKC_OUTPUTS = ['', ' OUT']
CLOCK_SETTINGS = []
for f in CLOCK_FORCED:
    for s in CLOCK_SOURCES:
        for o in CLOKC_OUTPUTS:
            CLOCK_SETTINGS.append(f"{f}{s}{o}")

# Trigger config byte: bit0 enable, bits2:1 edge, bits4:3 pull (FW trigger.h)
TRIGGER_EDGES = ['Rising', 'Falling', 'Both']
TRIGGER_PULLS = ['No-Pull', 'Pull-Up', 'Pull-Down']
TRIGGER_SETTINGS = ['Disabled']  # index 0 = trigger disabled
for e in TRIGGER_EDGES:
    for p in TRIGGER_PULLS:
        TRIGGER_SETTINGS.append(f"{e} {p}")

def trigger_index_to_byte(index: int) -> int:
    """TRIGGER_SETTINGS list index -> FW config byte."""
    if index <= 0:
        return 0  # disabled
    edge = (index - 1) // len(TRIGGER_PULLS)
    pull = (index - 1) % len(TRIGGER_PULLS)
    return 0x01 | ((edge & 0x03) << 1) | ((pull & 0x03) << 3)

def trigger_byte_to_index(b: int) -> int:
    """FW config byte -> TRIGGER_SETTINGS list index."""
    if not (b & 0x01):
        return 0
    edge = (b >> 1) & 0x03
    pull = (b >> 3) & 0x03
    return 1 + edge * len(TRIGGER_PULLS) + pull

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

# CRC-16/CCITT checksum and packet struct layouts are provided by the shared fdds
# core (fdds.crc.crc16_ccitt imported above, fdds.protocol.STRUCT below).
CRC_STRUCT = STRUCT.CRC

def _verify_crc(pkt: bytes) -> bytes|None:
    if not pkt or len(pkt)<2:
        return None
    data, recv_crc = pkt[:-2], CRC_STRUCT.unpack(pkt[-2:])[0]
    return data if crc16_ccitt(data)==recv_crc else False

def _signed_u16_delta(new: int, old: int) -> int:
        return ((new - old + 0x8000) & 0xFFFF) - 0x8000

# Packet struct layouts (shared fdds core; TRIGGER has no shared layout yet so stays local)
ID_HEADER_STRUCT = STRUCT.ID            # <HH HBB HBBI3I HBBI HH (last HH = channels_count + _reserved)
CHANNEL_HEADER_STRUCT = STRUCT.CHANNEL  # <4s ff (unit, offset, gain)
DATA_HEADER_STRUCT = STRUCT.DATA_HEADER # <HHII (packet_type, packet_num, ptp_seconds, ptp_nanoseconds)
TRIGGER_PACKET_STRUCT = struct.Struct("<HHB3xII") # packet_type, packet_num, sample_num, ptp_seconds, ptp_nanoseconds

# Precomputed vectors for vectorised DATA-packet parsing (Phase 2 fast path).
_DATA_SAMPLE_INDEX = np.arange(SAMPLES_PER_PACKET, dtype=np.int64)
# Per-sample PTP step back from the packet's last-sample timestamp.
_DATA_PTP_BACK_STEPS = np.arange(SAMPLES_PER_PACKET - 1, -1, -1, dtype=np.int64) * NS_PER_SAMPLE

def parse_id_packet(data):
    if len(data) < ID_HEADER_STRUCT.size:
        raise ValueError("[ERR]: ID packet is short")
    unpacked = ID_HEADER_STRUCT.unpack(data[:ID_HEADER_STRUCT.size])
    fields = ('packet_type',
              'state',
              'fw_id',
              'fw_ver_major',
              'fw_ver_minor',
              'hw_id',
              'hw_ver_major',
              'hw_ver_minor',
              'mcu_serial',
              'cpu_uid0',
              'cpu_uid1',
              'cpu_uid2',
              'adc_hw_id',
              'adc_ver_major',
              'adc_ver_minor',
              'adc_serial',
              'channels_count',
              'reserved')
    channels_info = ('unit', 'offset', 'gain')
    info = dict(zip(fields, unpacked))
    info['cpu_uid'] = (info.pop('cpu_uid0'), info.pop('cpu_uid1'), info.pop('cpu_uid2'))
    # channels_count may exceed the number of channel_info entries the device
    # actually ships (e.g. the CCU reports fault_output_get_count() but only
    # carries ACQUISITION_CHANNELS channel_info slots), so only parse the
    # entries that are present to avoid reading past the packet.
    available_channels = (len(data) - ID_HEADER_STRUCT.size) // CHANNEL_HEADER_STRUCT.size
    parsed_channels = max(0, min(info['channels_count'], available_channels))
    info['channels'] = [dict(zip(channels_info, CHANNEL_HEADER_STRUCT.unpack(data[ID_HEADER_STRUCT.size+i*CHANNEL_HEADER_STRUCT.size:ID_HEADER_STRUCT.size+(i+1)*CHANNEL_HEADER_STRUCT.size]))) for i in range(parsed_channels)]
    return info

class NumpyRing:
    """Fixed-capacity ring buffer backed by a preallocated numpy array.

    Drop-in replacement for the per-sample ``deque`` columns of ``DeviceBuffer``:
    supports ``extend(seq)``, ``clear()``, ``len()``/``bool()``, iteration (yields
    python scalars in arrival order) and ``np.asarray()``/``np.array()`` (returns
    the ordered contiguous array, optionally cast to a dtype).

    The backing store is allocated once via ``np.empty`` so the OS commits resident
    pages lazily (memory grows only as data is written) while still guaranteeing a
    fixed worst-case footprint with no reallocation/copy stalls in the packet hot
    path. Reading is a single contiguous copy instead of the O(N) python-level
    iteration that ``np.array(deque)`` performs, which is the main plot/CSV speedup.
    """
    __slots__ = ('_buf', '_cap', '_start', '_count', '_dtype')

    def __init__(self, capacity:int, dtype):
        self._cap = max(1, int(capacity))
        self._dtype = np.dtype(dtype)
        self._buf = np.empty(self._cap, dtype=self._dtype)
        self._start = 0
        self._count = 0

    def __len__(self):
        return self._count

    def __bool__(self):
        return self._count > 0

    def clear(self):
        self._start = 0
        self._count = 0

    def extend(self, seq):
        src = np.asarray(seq, dtype=self._dtype)
        n = src.size
        if n == 0:
            return
        cap = self._cap
        if n >= cap:
            # Only the most recent ``cap`` elements survive (matches deque(maxlen)).
            self._buf[:] = src[-cap:]
            self._start = 0
            self._count = cap
            return
        end = (self._start + self._count) % cap   # one past the logical end
        first = min(n, cap - end)
        self._buf[end:end + first] = src[:first]
        if first < n:                              # wrapped around the end
            self._buf[:n - first] = src[first:]
        new_count = self._count + n
        if new_count > cap:                        # overwrote the oldest elements
            self._start = (self._start + (new_count - cap)) % cap
            self._count = cap
        else:
            self._count = new_count

    def extend_const(self, value, n:int):
        """Append ``n`` copies of a single ``value`` (equivalent to
        ``extend([value]*n)`` but without building the intermediate python list).
        Used for the per-channel error column where one packet contributes the same
        parity-error count to every sample of the packet."""
        if n <= 0:
            return
        cap = self._cap
        if n >= cap:
            self._buf[:] = value
            self._start = 0
            self._count = cap
            return
        end = (self._start + self._count) % cap
        first = min(n, cap - end)
        self._buf[end:end + first] = value
        if first < n:
            self._buf[:n - first] = value
        new_count = self._count + n
        if new_count > cap:
            self._start = (self._start + (new_count - cap)) % cap
            self._count = cap
        else:
            self._count = new_count

    def _ordered(self):
        """Contiguous copy of the valid elements in arrival (oldest-first) order."""
        if self._count == 0:
            return np.empty(0, dtype=self._dtype)
        cap = self._cap
        end = self._start + self._count
        if end <= cap:
            return self._buf[self._start:end].copy()
        return np.concatenate((self._buf[self._start:], self._buf[:end - cap]))

    def __array__(self, dtype=None, copy=None):
        arr = self._ordered()
        if dtype is not None:
            arr = arr.astype(dtype, copy=False)
        return arr

    def __iter__(self):
        # Yield python scalars in arrival order (matches deque iteration so list()
        # and slicing behave identically to the previous deque-backed columns).
        return iter(self._ordered().tolist())

# Buffer container
@dataclass
class DeviceBuffer:
    def __init__(self, channels:int=3):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.lock = threading.Lock()
        # Per-sample columns backed by preallocated numpy ring buffers. signal[0] is
        # the per-sample time index (int64, large range); signal[1..] are the int16
        # ADC/bit samples; error[*] are the small per-channel parity-error counts.
        self.time   = NumpyRing(BUFFER_SIZE, np.int64)
        self.signal = [NumpyRing(BUFFER_SIZE, np.int64)] + [NumpyRing(BUFFER_SIZE, np.int16) for _ in range(channels)]
        self.error  = [NumpyRing(BUFFER_SIZE, np.int16) for _ in range(channels)]
        # PTP timestamp per row in integer nanoseconds (mirrors the packet PTP time)
        self.ptp    = NumpyRing(BUFFER_SIZE, np.int64)
        # CCU RESULT-packet metadata (one entry per result packet; empty for nodes).
        # These hold python tuples/ints (one per packet, not per sample) so they stay
        # as deques rather than numeric rings.
        self.result_fault_state    = deque(maxlen=BUFFER_SIZE)  # tuple per row: per-node fault_state[GATHERING_DEVICES]
        self.result_parity_errors  = deque(maxlen=BUFFER_SIZE)  # tuple per row: parity_errors[GATHERING_DEVICES][ACQUISITION_CHANNELS]
        self.result_crc_error_mask = deque(maxlen=BUFFER_SIZE)  # int per row
        # Monotonic change counter bumped on every append; lets the plot skip a
        # redraw when nothing new has arrived (e.g. a finished trigger/new-sampling
        # capture that already holds all the data it wanted).
        self.revision = 0

    def extend(self, t:List[int], samples:List[List[int]], errs:List[int], ptp:List[int]):
        with self.lock:
            self.time.extend(t)
            for ch, sig in enumerate(samples):
                self.signal[ch+1].extend(sig)
                self.error[ch].extend_const(errs[ch], len(sig))
            self.signal[0].extend(t)
            self.ptp.extend(ptp)
            self.revision += 1

    def extend_result(self, t:List[int], samples:List[List[int]], errs:List[int], ptp:List[int],
                      fault_state:Tuple[int, ...], parity_errors:Tuple[int, ...], crc_error_mask:int):
        with self.lock:
            self.time.extend(t)
            for ch, sig in enumerate(samples):
                self.signal[ch+1].extend(sig)
                self.error[ch].extend_const(errs[ch], len(sig))
            self.signal[0].extend(t)
            self.ptp.extend(ptp)
            self.result_fault_state.extend([fault_state]*len(t))
            self.result_parity_errors.extend([parity_errors]*len(t))
            self.result_crc_error_mask.extend([crc_error_mask]*len(t))
            self.revision += 1

# Async UDP socket
class AsyncSocket:
    def __init__(self, loop, local_port:int, label:str, backend: str = DEFAULT_SOCKET_BACKEND,
                 rcvbuf: int = DATA_SOCKET_RCVBUF_BYTES):
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

        # Grow the OS UDP receive buffer so a synchronous processing burst (the
        # trigger flush) cannot overflow it and drop packets.
        if rcvbuf and hasattr(self.sock, 'set_recv_buffer'):
            try:
                applied = self.sock.set_recv_buffer(rcvbuf)
                self._logger.info(f'AsyncSocket SO_RCVBUF requested={rcvbuf} applied={applied}')
            except Exception as e:
                self._logger.warning(f'Failed to set SO_RCVBUF={rcvbuf}: {e}')

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
        self.pretrigger_packets = 0
        self.trigger_sample_num = 0
        self.waiting_for_trigger = False
        self.trigger_mode = False
        self.immediate_trigger = False

    def fire_trigger(self, trigger_order:int|None = None):
        if not self.enabled:
            return
        if not self.trigger_mode:
            return
        if not self.waiting_for_trigger:
            return
        self.waiting_for_trigger = False
        self.device_manager.ptp_trigger(trigger_order)

ptp_mode = PTPMode()

# Single device client
class Device:
    # Packet type ids sourced from the shared fdds PACKET enum
    PKT_TYPE_ACK = int(PACKET.ACK)
    PKT_TYPE_ID = int(PACKET.ID)
    PKT_TYPE_DATA = int(PACKET.DATA)
    PKT_TYPE_TRIGGER = int(PACKET.TRIGGER)
    PKT_TYPE_LOG  = int(PACKET.LOG)
    PKT_TYPE_RESULT  = int(PACKET.RESULT)

    def __init__(self, ip:str, cmd_port:int, data_port:int, loop, manager=None):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.ip, self.cmd_port, self.data_port, self.loop = ip,cmd_port,data_port,loop
        self.manager = manager
        self.channels = 2
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(RECV_TIMEOUT_S)
        self.cmd_sock.connect((ip, cmd_port))
        self.buffer = DeviceBuffer(self.channels)
        self.id = int(ip.split('.')[3])
        self.header_struct = struct.Struct('<HH')
        self.silent_ping = False
        self.capture_active = False
        self.capture_counter = 0
        self.capture_limit = 0
        self.ptp_triggered = False
        self.received_last = 0
        self.last_data_time = 0.0
        self.last_ack_time = 0.0
        self.input_packet_ring = deque(maxlen=PTP_TRIGGER_RING_PACKETS)
        self.first_data_order = None
        self.last_data_order = None
        self.packet_index = 0
        self.trigger_order = None
        self.trigger_sample_num = 0
        self.pretrigger_packets = 0

    def _send_cmd(self, code:int, payload:bytes=b'', expect:bool=True, socket_ = None, port:int|None = None):
        pkt = struct.pack('<I', code) + payload
        if socket_ is None:
            self.cmd_sock.send(pkt)
        else:
            socket_.sendto(pkt, (self.ip, port or self.cmd_port))
        if not expect:
            return None
        try:
            return (socket_ or self.cmd_sock).recv(2048)
        except socket.timeout:
            return None
        
    def ping(self, socket_=None, silent=False, port:int|None = None)->bool:
        self.silent_ping = silent
        return bool(self._send_cmd(0, struct.pack('?', silent), expect=socket_ is None, socket_=socket_, port=port))

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
            self.buffer = DeviceBuffer(self.channels)
            self.info = info
            return info
        except Exception as e:
            self._logger.warning(f"Dev {self.ip} failed to parse ID packet: {e}")
            return None

    def get_id(self)->dict|None:
        return self._parse_id(self._send_cmd(1) or b'')

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
        self.trigger_order = trigger_order
        self.trigger_sample_num = ptp_mode.trigger_sample_num
        self.pretrigger_packets = ptp_mode.pretrigger_packets
        if trigger_order is not None:
            self.flush_input_packet_ring(trigger_order, ptp_mode.pretrigger_packets)

    def trigger_sample_index(self):
        """Linear sample index (in buffer.signal[0] units) of the trigger sample
        (t = 0), or None when no trigger reference is available. Derived from the
        packet order of the trigger packet, so it is independent of how many
        pre-/post-trigger packets were actually captured."""
        if self.trigger_order is None or self.first_data_order is None:
            return None
        return _signed_u16_delta(self.trigger_order, self.first_data_order) * SAMPLES_PER_PACKET + self.trigger_sample_num

    def ptp_wait_trigger(self):
        # Do NOT clear the input ring here: arming only enables the hardware
        # trigger (cmd 20). The ring is a continuously rolling history of the last
        # PTP_TRIGGER_RING_PACKETS packets and must stay intact so the pre-trigger
        # window is filled from packets that arrived *before* arming, not only from
        # the (variable) interval between arming and the trigger event.
        return self._send_cmd(20)

    def ptp_reset(self, keep_ring: bool = False):
        self.capture_active = False
        self.capture_counter = 0
        self.capture_limit = 0
        self.ptp_triggered = False
        if not keep_ring:
            self.input_packet_ring.clear()
        self.first_data_order = None
        self.last_data_order = None
        self.packet_index = 0
        self.trigger_order = None
        self.trigger_sample_num = 0
        self.pretrigger_packets = 0
    
    def begin_capture(self, n: int):
        self.capture_active = True
        self.capture_limit = n
        self.capture_counter = 0
        self.first_data_order = None
        self.last_data_order = None
        self.packet_index = 0
    
    def flush_input_packet_ring(self, trigger_order:int, pretrigger_packets:int):
        """Replay buffered packets around the trigger from the input ring.

        Processes, in chronological order:
          * exactly ``pretrigger_packets`` packets immediately before the trigger,
          * the trigger packet itself (order == trigger_order), if it is already
            buffered (it may instead arrive live, just after the TRIGGER packet),
          * any packets that arrived just after the trigger but before this flush
            ran (so the pre/post boundary has no missing-packet gap).
        """
        pre = []   # (distance_back, pkt) for order <  trigger_order
        trig = []  # pkt for order == trigger_order
        post = []  # (distance_fwd, pkt) for order >  trigger_order
        for pkt in self.input_packet_ring:
            order = self.header_struct.unpack(pkt[:4])[1]
            back = (trigger_order - order) & 0xFFFF
            if back == 0:
                trig.append(pkt)
            elif back < 0x8000:
                pre.append((back, pkt))
            else:
                post.append(((order - trigger_order) & 0xFFFF, pkt))
        self.input_packet_ring.clear()
        pre.sort(key=lambda e: e[0])
        pre = pre[:pretrigger_packets]     # exactly N packets before the trigger
        pre.reverse()                      # chronological order (oldest first)
        post.sort(key=lambda e: e[0])      # chronological order
        ordered = [pkt for _, pkt in pre] + trig + [pkt for _, pkt in post]
        for buffered_pkt in ordered:
            if self.capture_limit > 0 and self.capture_counter >= self.capture_limit:
                return
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

    def set_trigger_config(self, config_byte:int, holdoff_ns:int = 0, save: bool = False):
        """CMD_SET_TRIGGER_CONFIG: config byte + holdoff (ns). Optional 0xAC to persist."""
        payload = struct.pack('<BQ', config_byte & 0xFF, holdoff_ns & 0xFFFFFFFFFFFFFFFF)
        if save:
            payload += struct.pack('<B', 0xAC)
        return self._send_cmd(36, payload)

    def get_trigger_config(self):
        """CMD_GET_TRIGGER_CONFIG -> (config_byte, holdoff_ns) or None."""
        pkt = self._send_cmd(37)
        if not pkt:
            self._logger.warning(f"Dev {self.ip} failed to get trigger config.")
            return None
        if (l:=len(pkt)) < 17:
            self._logger.warning(f"Dev {self.ip} failed to get trigger config - too short packet ({l}).")
            return None
        packet_type, state, cmd, config_byte, holdoff_ns = struct.unpack('<HHIBQ', pkt[:17])
        if packet_type != self.PKT_TYPE_ACK:
            self._logger.warning(f"Dev {self.ip} failed to get trigger config - unexpected packet type ({packet_type}).")
            return None
        if cmd != 37:
            self._logger.warning(f"Dev {self.ip} failed to get trigger config - unexpected command ({cmd}).")
            return None
        return (config_byte, holdoff_ns)

    def _send_cmd_with_ack(self, code:int, payload:bytes=b''):
        """Send a command and return (state, extra_bytes) from the ACK, or None."""
        resp = self._send_cmd(code, payload)
        if not resp or len(resp) < 8:
            return None
        packet_type, state, ack_cmd = struct.unpack('<HHI', resp[:8])
        if packet_type != self.PKT_TYPE_ACK or ack_cmd != code:
            return None
        return (state, resp[8:])

    def system_startup_start(self, samples:int=0) -> bool:
        """CMD_STARTUP_CONTROL start (sub=1). samples=0 => infinite."""
        return self._send_cmd_with_ack(CMD_STARTUP_CONTROL, struct.pack('<BI', 1, samples)) is not None

    def system_startup_abort(self) -> bool:
        """CMD_STARTUP_CONTROL abort (sub=0)."""
        return self._send_cmd_with_ack(CMD_STARTUP_CONTROL, struct.pack('<B', 0)) is not None

    def system_stop(self) -> bool:
        """CMD_STOP_SYSTEM."""
        return self._send_cmd_with_ack(CMD_STOP_SYSTEM) is not None

    def get_system_state(self) -> dict | None:
        """CMD_GET_SYSTEM_STATE — CCU startup/gathering state."""
        res = self._send_cmd_with_ack(CMD_GET_SYSTEM_STATE)
        if res is None:
            return None
        _state, extra = res
        if len(extra) < 12:
            return None
        return {
            'gathering_state': struct.unpack_from('<H', extra, 0)[0],
            'startup_state':   extra[2],
            'startup_error':   extra[3],
            'freq_hz':         struct.unpack_from('<I', extra, 4)[0],
            'packets':         struct.unpack_from('<I', extra, 8)[0],
            'err_node':        extra[12] if len(extra) >= 13 else 0,
            'err_value':       struct.unpack_from('<I', extra, 14)[0] if len(extra) >= 18 else 0,
        }

    def get_receivers(self, receiver_type:int=RECEIVER_TYPE_DATA) -> dict | None:
        """CMD_GET_RECEIVERS — list registered receivers of the given type.

        Returns {'active': [(ip, port), ...], 'eeprom': (ip, port)|None} or None.
        Request payload mirrors the register layout: type byte at offset 6.
        """
        payload = b'\x00' * 6 + struct.pack('B', receiver_type)
        res = self._send_cmd_with_ack(CMD_GET_RECEIVERS, payload)
        if res is None:
            return None
        _state, extra = res
        if len(extra) < 8:
            return None
        ram_count = extra[0]
        eeprom_valid = extra[1]
        eeprom = None
        if eeprom_valid:
            eeprom = (socket.inet_ntoa(extra[2:6]), struct.unpack_from('<H', extra, 6)[0])
        active = []
        base = 8
        for i in range(min(ram_count, MAX_RECEIVERS)):
            off = base + i * 6
            if off + 6 > len(extra):
                break
            active.append((socket.inet_ntoa(extra[off:off+4]),
                           struct.unpack_from('<H', extra, off+4)[0]))
        return {'active': active, 'eeprom': eeprom}

    def get_acquisition_state(self) -> dict | None:
        """CMD_GET_ACQUISITION_STATE — per-device clock/PTP/packet state."""
        res = self._send_cmd_with_ack(CMD_GET_ACQUISITION_STATE)
        if res is None:
            return None
        _state, extra = res
        if len(extra) < 12:
            return None
        state_flags, ptp_locked, _reserved = struct.unpack_from('<HBB', extra, 0)
        freq_hz, packets = struct.unpack_from('<II', extra, 4)
        return {
            'state_flags':     state_flags,
            'ptp_sync_locked': bool(ptp_locked),
            'freq_hz':         freq_hz,
            'packets':         packets,
        }

    def on_raw_packet(self, pkt:bytes):
        typ, order = self.header_struct.unpack(pkt[:4])
        match typ:
            case self.PKT_TYPE_ACK:
                self.last_ack_time = time.monotonic()
                if not self.silent_ping:
                    self._logger.info(f"Dev {self.ip} received ACK on DATA socket.")
                return
            case self.PKT_TYPE_DATA:
                self.last_data_time = time.monotonic()
                if ptp_mode.enabled:
                    if not self.capture_active:
                        if ptp_mode.immediate_trigger and ptp_mode.waiting_for_trigger:
                            # Software (immediate) trigger: this packet is t = 0; the
                            # pre-trigger packets come from the continuously buffered ring.
                            ptp_mode.trigger_sample_num = 0
                            ptp_mode.immediate_trigger = False
                            ptp_mode.fire_trigger(order)
                            # fall through so this packet is captured as the first post-trigger packet
                        else:
                            self.input_packet_ring.append(bytes(pkt))
                            return

                    if self.capture_limit > 0 and self.capture_counter >= self.capture_limit:
                        if self.capture_active:
                            self._logger.info(
                                f"PTP capture done dev {self.id} (DATA): trigger_order={self.trigger_order}, "
                                f"first={self.first_data_order}, last={self.last_data_order}, "
                                f"count={self.capture_counter}/{self.capture_limit}")
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
                # off 4 = uint32 ptp_seconds
                # off 8 = uint32 ptp_nanoseconds
                ptp_seconds, ptp_nanoseconds = struct.unpack('<II', data[4:12])
                off = 12 # off 12 = data
                if self.first_data_order is None:
                    self.first_data_order = order
                    self.last_data_order = order
                    self.packet_index = 0
                else:
                    delta = _signed_u16_delta(order, self.last_data_order)
                    if delta > 1000:
                        self._logger.warning(
                            f"Dev {self.ip} large packet jump: order={order}, "
                            f"last={self.last_data_order}, delta={delta}"
                        )
                    self.packet_index += delta
                    self.last_data_order = order
                
                rel_order = self.packet_index
                base = rel_order*SAMPLES_PER_PACKET
                t = (base + _DATA_SAMPLE_INDEX).tolist()
                # The packet PTP timestamp marks the last sample in the window; earlier
                # samples are NS_PER_SAMPLE older each.
                ptp_last_ns = ptp_seconds*1_000_000_000 + ptp_nanoseconds
                ptp = (ptp_last_ns - _DATA_PTP_BACK_STEPS).tolist()
                # Vectorised int16 sample extraction for all channels at once.
                nsamp = self.channels*SAMPLES_PER_PACKET
                samples = np.frombuffer(data, dtype='<i2', count=nsamp, offset=off).reshape(self.channels, SAMPLES_PER_PACKET).tolist()
                off += 2*nsamp
                errs = list(data[off:off+self.channels])
                self.loop.call_soon_threadsafe(self.buffer.extend, t, samples, errs, ptp)
                return order
            case self.PKT_TYPE_TRIGGER:
                _, packet_num, sample_num, ptp_seconds, ptp_nanoseconds = TRIGGER_PACKET_STRUCT.unpack(pkt[:TRIGGER_PACKET_STRUCT.size])
                if self.manager is not None and self.manager.trigger_signal is not None:
                    self.manager.trigger_signal.emit(self.ip)
                ptp_mode.trigger_sample_num = sample_num
                ptp_mode.fire_trigger(order)
                self._logger.info(f'PTP trigger received on {self.id} in packet {order} and sample {sample_num}, sent at {ptp_seconds}.{ptp_nanoseconds:09d} s.')
                return
            case self.PKT_TYPE_LOG:
                log_msg = pkt[4:].decode('utf-8').strip()
                self._logger.info(f"Dev {self.ip} log[{order}]: {log_msg}")
                return
            case self.PKT_TYPE_RESULT:
                self.last_data_time = time.monotonic()
                if ptp_mode.enabled:
                    if not self.capture_active:
                        if ptp_mode.immediate_trigger and ptp_mode.waiting_for_trigger:
                            # Software (immediate) trigger: this packet is t = 0; the
                            # pre-trigger packets come from the continuously buffered ring.
                            ptp_mode.trigger_sample_num = 0
                            ptp_mode.immediate_trigger = False
                            ptp_mode.fire_trigger(order)
                            # fall through so this packet is captured as the first post-trigger packet
                        else:
                            self.input_packet_ring.append(bytes(pkt))
                            return

                    if self.capture_limit > 0 and self.capture_counter >= self.capture_limit:
                        if self.capture_active:
                            self._logger.info(
                                f"PTP capture done dev {self.id} (RESULT): trigger_order={self.trigger_order}, "
                                f"first={self.first_data_order}, last={self.last_data_order}, "
                                f"count={self.capture_counter}/{self.capture_limit}")
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
                if self.first_data_order is None:
                    self.first_data_order = order
                    self.last_data_order = order
                    self.packet_index = 0
                else:
                    delta = _signed_u16_delta(order, self.last_data_order)
                    if delta > 1000:
                        self._logger.warning(
                            f"Dev {self.ip} large packet jump: order={order}, "
                            f"last={self.last_data_order}, delta={delta}"
                        )
                    self.packet_index += delta
                    self.last_data_order = order
                
                rel_order = self.packet_index
                t = [rel_order]
                # result_packet_t header: packet_type, packet_num, ptp_seconds,
                # ptp_nanoseconds, value (the PTP time mirrors the node DATA packet's
                # first sample of this window).
                ptp_seconds, ptp_nanoseconds = struct.unpack('<II', data[4:12])
                ptp = [ptp_seconds*1_000_000_000 + ptp_nanoseconds]
                val_off = 12
                result_code = struct.unpack('<H', data[val_off:val_off+2])[0]
                samples = []

                for bit_idx in range(self.channels):
                    bit_vals = (result_code >> bit_idx) & 1
                    samples.append([bit_vals])

                # result_packet_t layout after value, derived from the shared
                # constants GATHERING_DEVICES and ACQUISITION_CHANNELS:
                #   fault_state[GATHERING_DEVICES]                   GATHERING_DEVICES x uint16
                #   parity_errors[GATHERING_DEVICES][ACQ_CHANNELS]   GATHERING_DEVICES*ACQ_CHANNELS x uint8
                #   crc_error_mask                                   uint16
                fs_off = val_off + 2
                pe_off = fs_off + GATHERING_DEVICES * 2
                cem_off = pe_off + GATHERING_DEVICES * ACQUISITION_CHANNELS
                fault_state = struct.unpack(f'<{GATHERING_DEVICES}H', data[fs_off:pe_off])
                parity_errors = tuple(data[pe_off:cem_off])
                crc_error_mask = struct.unpack('<H', data[cem_off:cem_off+2])[0]
                # errs feeds the per-channel GUI error label (first self.channels values used).
                errs = list(parity_errors)

                self.loop.call_soon_threadsafe(self.buffer.extend_result, t, samples, errs,
                                               ptp, fault_state, parity_errors, crc_error_mask)
                #self._logger.info(f"Dev {self.ip} packetNumber[{order}]: result {result_code}")
                return order
            case self.PKT_TYPE_ID:
                self._logger.info(f"Dev {self.ip} ID packet received on data socket.")
                self._parse_id(pkt)
                return

# Manager of multiple devices
class DeviceManager:
    MAX_DEVICES = 5
    def __init__(self, data_port:int = DEFAULT_DATA_PORT, socket_backend: str = DEFAULT_SOCKET_BACKEND,
                 data_rcvbuf: int = DATA_SOCKET_RCVBUF_BYTES):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self.data_port = data_port
        self.socket_backend = socket_backend
        self.data_rcvbuf = data_rcvbuf
        self.devices: Dict[str,Device] = {}
        self.loop = None
        self.data_socket = None
        self.dispatch_task = None
        self.loop_thread = None
        self.trigger_signal = None  # GUI pyqtSignal(str ip), emitted when a trigger packet arrives
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.cmd_sock.settimeout(RECV_TIMEOUT_S)
        self.cmd_sock.connect(('192.168.137.255', DEFAULT_CMD_PORT))

    def broadcast(self, method:str, *args, **kwargs):
        for dev in self.devices.values():
            getattr(dev, method)(*args, **kwargs)

    def attach_loop(self, loop):
        self.loop = loop
        self.data_socket = AsyncSocket(loop, self.data_port, 'data', backend=self.socket_backend,
                                       rcvbuf=self.data_rcvbuf)

    def set_loop_thread(self, loop_thread):
        self.loop_thread = loop_thread
    
    def _send_cmd_broadcast(self, code:int, payload:bytes=b'', expect:bool=True, socket_ = None):
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

    def set_trigger_signal(self, signal):
        """Register the GUI signal (pyqtSignal(str)) emitted with the source IP on trigger."""
        self.trigger_signal = signal

    def add_device(self, ip:str, cmd_port:int = DEFAULT_CMD_PORT):
        if len(self.devices) >= self.MAX_DEVICES or ip in self.devices:
            return
        self.devices[ip] = Device(ip, cmd_port, self.data_port, self.loop, manager=self)

    def ping_all(self):
        return {ip: dev.ping() for ip,dev in self.devices.items()}
    
    def penetrate_firewall(self, silent = True):
        self.broadcast("ping", self.data_socket, silent)

    def get_all_ids(self):
        return {ip: dev.get_id() for ip,dev in self.devices.items()}
    
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

    def get_trigger_config_all(self):
        return {ip: dev.get_trigger_config() for ip, dev in self.devices.items()}

    def reset_counter(self):
        return self._send_cmd_broadcast(10)      

    def force_trigger(self):
        #for dev in self.devices.values(): dev.force_trigger()
        self.devices[next(iter(self.devices))].force_trigger()

    def ptp_trigger(self, trigger_order:int|None = None):
        # The trigger packet (order == trigger_order) is the first post-trigger
        # packet; it is replayed from the input ring or arrives live and counts
        # against samples_awaited. The capture is bounded by a packet COUNT, so any
        # stray packet that consumes a slot without landing in the window (a UDP-
        # reordered pre-trigger packet arriving after the trigger, a duplicate, or a
        # bad-CRC packet) would truncate the tail. Capture a margin of extra packets
        # so the full window (incl. the last packet trigger_order+samples_awaited) is
        # always stored; the plot/CSV trim back to exactly samples_awaited.
        extra = TRIGGER_CAPTURE_MARGIN_PACKETS
        capture_limit = max(0, ptp_mode.samples_awaited + ptp_mode.pretrigger_packets + extra)
        for idx, dev in enumerate(self.devices.values()):
            dev.trigger_order = trigger_order
            # The CCU emits its result one packet behind the nodes' data, so grant
            # it one more packet to fill the same post window (the plot/CSV trim it).
            dev.begin_capture(capture_limit + (1 if idx == CCU_DEVICE_INDEX else 0))
            dev.ptp_trigger(trigger_order)
    
    def ptp_wait_trigger(self):
        #for dev in self.devices.values(): dev.ptp_wait_trigger()
        self.devices[next(iter(self.devices))].ptp_wait_trigger()
    
    def ptp_reset(self, keep_ring: bool = False):
        for dev in self.devices.values(): dev.ptp_reset(keep_ring=keep_ring)
    
    def begin_capture_all(self, n: int):
        for dev in self.devices.values(): dev.begin_capture(n)

    def ccu_device(self):
        """Return the CCU device (CCU_DEVICE_INDEX) or None if not applied yet."""
        devs = list(self.devices.values())
        if 0 <= CCU_DEVICE_INDEX < len(devs):
            return devs[CCU_DEVICE_INDEX]
        return None

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
    # emits (ip) when a trigger packet arrives from the given device
    trigger_received = pyqtSignal(str)

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
        self.sampling_indicator_button = None
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
        self.device_trigger_settings: List[QComboBox] = []
        self.device_trigger_holdoff: List[QSpinBox] = []

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

            trig_box = QHBoxLayout()
            cb_trigger = QComboBox()
            cb_trigger.addItems(TRIGGER_SETTINGS)
            cb_trigger.setCurrentIndex(0)
            cb_trigger.currentIndexChanged.connect(lambda _idx, row=i: self._update_trigger_settings(row))
            trig_box.addWidget(cb_trigger)
            self.device_trigger_settings.append(cb_trigger)
            sb_holdoff = QSpinBox()
            sb_holdoff.setRange(0, 1_000_000_000)
            sb_holdoff.setSuffix(' us')
            sb_holdoff.setValue(0)
            sb_holdoff.editingFinished.connect(lambda row=i: self._update_trigger_settings(row))
            trig_box.addWidget(sb_holdoff)
            self.device_trigger_holdoff.append(sb_holdoff)
            cfg.addLayout(trig_box, i, 5)

        self.leader_buttons.buttonClicked[int].connect(self._leader_changed)

        cfg.addWidget(QLabel('Receiver addr:port'), 0, 6)
        self.receiver_edit = QLineEdit(f'0.0.0.0:{DEFAULT_DATA_PORT}')
        cfg.addWidget(self.receiver_edit, 0, 7)

        cfg.addWidget(QLabel('Measurement number'), 2, 6)
        self.measurement_number_edit = QLineEdit(f'0')
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

        self.save_trigger_config_btn = QPushButton('Save trigger config')
        cfg.addWidget(self.save_trigger_config_btn, DeviceManager.MAX_DEVICES, 5)
        self.save_trigger_config_btn.clicked.connect(self._save_trigger_config)

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

        # System startup/stop control + state monitoring
        self.system_start_btn = QPushButton('Start System')
        self.system_start_btn.clicked.connect(self._system_start)
        btns.addWidget(self.system_start_btn)
        self.system_stop_btn = QPushButton('Stop System')
        self.system_stop_btn.clicked.connect(self._system_stop)
        btns.addWidget(self.system_stop_btn)
        self.system_status_lbl = QLabel('System: —')
        self.system_status_lbl.setStyleSheet('font-family: monospace')
        btns.addWidget(self.system_status_lbl)

        btns.addWidget(QLabel('Pre-trigger packets:'))
        self.pretrigger_spin = QSpinBox()
        self.pretrigger_spin.setRange(0, PTP_TRIGGER_RING_PACKETS)
        self.pretrigger_spin.setValue(0)
        btns.addWidget(self.pretrigger_spin)

        btns.addWidget(QLabel('Post-trigger packets:'))
        self.sample_spin = QSpinBox()
        self.sample_spin.setRange(0, MAX_CAPTURE_PACKETS)
        self.sample_spin.setValue(10)
        btns.addWidget(self.sample_spin)
        # Keep pre-trigger + post-trigger packets within the sample buffer capacity.
        self.pretrigger_spin.valueChanged.connect(self._update_capture_limits)
        self._update_capture_limits()

        self.start_sampling_btn = QPushButton('Start New Sampling')
        self.start_sampling_btn.clicked.connect(self._start_new_sampling)
        btns.addWidget(self.start_sampling_btn)

        self.start_sampling_trigger_btn = QPushButton('Start New Sampling on trigger')
        self.start_sampling_trigger_btn.clicked.connect(self._start_new_sampling_on_trigger)
        btns.addWidget(self.start_sampling_trigger_btn)

        for label, fn in (('Save Measurement'               , self.save_measurement                 ),
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

        # System monitoring state
        self._system_poll_count = 0
        self._system_stalled = False
        self._system_stall_status_base = 'System: data stalled'
        self._system_diag_pending = set()
        self._system_diag_firewall = False
        self._system_diag_unresponsive = False
        self._system_diag_keep_status = False
        self._system_status_timer = QTimer(self)
        self._system_status_timer.setInterval(SYSTEM_STATUS_POLL_INTERVAL_MS)
        self._system_status_timer.timeout.connect(self._system_poll_status)
        self._system_watchdog_timer = QTimer(self)
        self._system_watchdog_timer.setInterval(SYSTEM_WATCHDOG_INTERVAL_MS)
        self._system_watchdog_timer.timeout.connect(self._system_watchdog)

        # Plot area with legend
        self.plot_widget = pg.GraphicsLayoutWidget()
        root.addWidget(self.plot_widget)
        self.ax = self.plot_widget.addPlot(title='Signals – device×channel')
        self.ax.showGrid(x=True,y=True,alpha=0.3)
        self.ax.setLabel('bottom','Time',units='s')
        self.ax.setLabel('left','Amplitude')
        self.ax.addLegend()
        self.curves: Dict[Tuple[str,int], pg.PlotDataItem] = {}
        # Last per-device buffer.revision drawn; used to skip redundant redraws when
        # no new data has arrived since the previous frame. None forces the first draw.
        self._plot_revisions = None

        # Detection plot
        self.plot_widget.nextRow()  # move to next row in the graphics layout
        self.ax_result = self.plot_widget.addPlot(title='Detection result')
        self.ax_result.showGrid(x=True, y=True, alpha=0.5)
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

        self.data_ready.connect(self._check_order)
        self.trigger_received.connect(self._flash_device_trigger)
        self.manager.set_trigger_signal(self.trigger_received)

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
        for chk, le in zip(self.device_checks,self.device_edits):
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
                self.manager.add_device(ip,port)
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

    def _set_sampling_indicator(self, button: QPushButton | None):
        for b in (self.start_sampling_btn, self.start_sampling_trigger_btn):
            b.setStyleSheet("")

        self.sampling_indicator_button = button
        if button is not None:
            button.setStyleSheet("background-color: #ffd84d; color: black;")

    def _clear_sampling_indicator_if_complete(self, received_counts: List[int]):
        if self.sampling_indicator_button is None or self.expected_samples <= 0:
            return
        if received_counts and all(received >= self.expected_samples for received in received_counts):
            self._set_sampling_indicator(None)

    def _update_capture_limits(self):
        """Cap post-trigger packets so pre-trigger + post-trigger fit the sample buffer."""
        pre = self.pretrigger_spin.value()
        self.sample_spin.setMaximum(max(0, MAX_CAPTURE_PACKETS - pre))

    def _start_sampling(self):
        n = self.sample_spin.value()
        pretrigger_packets = self.pretrigger_spin.value()
        if n + pretrigger_packets > MAX_CAPTURE_PACKETS:
            self._logger.warning(f"Pre-trigger ({pretrigger_packets}) + post-trigger ({n}) packets exceed buffer capacity {MAX_CAPTURE_PACKETS}, clamping post-trigger.")
            n = max(0, MAX_CAPTURE_PACKETS - pretrigger_packets)
            self.sample_spin.setValue(n)
        self.expected_samples = n + pretrigger_packets

        if ptp_mode.enabled:
            # Immediate software trigger: the click instant becomes t = 0. Pre-trigger
            # packets are replayed from the continuously buffered input ring and post-
            # trigger packets are captured live, so the record has the same length and
            # time layout (-pretrigger .. 0 .. +post) as 'Start Sampling on trigger'.
            self.manager.ptp_reset(keep_ring=True)
            ptp_mode.samples_awaited = n
            ptp_mode.pretrigger_packets = pretrigger_packets
            ptp_mode.trigger_sample_num = 0
            ptp_mode.trigger_mode = True
            ptp_mode.immediate_trigger = True
            ptp_mode.waiting_for_trigger = True
            self._logger.info(f'Started PTP sampling (n={n}, pretrigger_packets={pretrigger_packets})')
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
        pretrigger_packets = self.pretrigger_spin.value()
        if n + pretrigger_packets > MAX_CAPTURE_PACKETS:
            self._logger.warning(f"Pre-trigger ({pretrigger_packets}) + post-trigger ({n}) packets exceed buffer capacity {MAX_CAPTURE_PACKETS}, clamping post-trigger.")
            n = max(0, MAX_CAPTURE_PACKETS - pretrigger_packets)
            self.sample_spin.setValue(n)
        self.expected_samples = n + pretrigger_packets

        if ptp_mode.enabled:
            # Keep the rolling pre-trigger ring (and the device packet counter, see
            # _start_new_sampling_on_trigger) so the pre-trigger window is filled
            # from the continuous stream that preceded arming.
            self.manager.ptp_reset(keep_ring=True)

            ptp_mode.waiting_for_trigger = True
            ptp_mode.trigger_mode = True
            ptp_mode.samples_awaited = n
            ptp_mode.pretrigger_packets = pretrigger_packets

            self.manager.ptp_wait_trigger()
            self._logger.info(f'Wait trigger PTP sampling (n={n}, pretrigger_packets={pretrigger_packets})')
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
        self._set_sampling_indicator(self.start_sampling_btn)
        # In PTP mode keep the device packet counter running so the pre-trigger ring
        # stays continuous with the live stream; only the legacy (non-PTP) path resets.
        if not ptp_mode.enabled:
            self._reset_counter()
        self.clear_plot()
        QTimer(self).singleShot(100, self._start_sampling)

    def _start_new_sampling_on_trigger(self):
        self._set_sampling_indicator(self.start_sampling_trigger_btn)
        # In PTP mode keep the device packet counter running so the pre-trigger ring
        # stays continuous with the live stream; only the legacy (non-PTP) path resets.
        if not ptp_mode.enabled:
            self._reset_counter()
        self.clear_plot()
        QTimer(self).singleShot(100, self._start_sampling_on_trigger)

    def _stop_sampling(self):
        self.manager.broadcast('stop_sampling')
        self._set_sampling_indicator(None)
        self._logger.info('Stopped all sampling')

    # ----- System startup/stop control + state monitoring ---------------------
    def _system_ccu(self):
        """Return the CCU device (CCU_DEVICE_INDEX) or None, logging a warning."""
        dev = self.manager.ccu_device()
        if dev is None:
            self._logger.warning('No CCU device available (apply devices first).')
        return dev

    def _set_system_status(self, text, color=''):
        """Set the system status label text and background colour."""
        self.system_status_lbl.setText(text)
        style = 'font-family: monospace'
        if color:
            style += f'; background-color: {color}'
        self.system_status_lbl.setStyleSheet(style)

    def _system_state_color(self, state):
        """Map a startup-state code to its status-label colour."""
        if state == SYSTEM_STATE_IDLE:
            return SYSTEM_STATUS_COLOR_IDLE
        if state == SYSTEM_STATE_RUNNING:
            return SYSTEM_STATUS_COLOR_OK
        if state == SYSTEM_STATE_FAILED:
            return SYSTEM_STATUS_COLOR_ERROR
        return SYSTEM_STATUS_COLOR_BUSY

    def _system_refresh_state(self):
        """Query the CCU system state once and react to it (used after Apply config)."""
        dev = self._system_ccu()
        if dev is None:
            self._set_system_status('System: no CCU', SYSTEM_STATUS_COLOR_ERROR)
            return
        st = dev.get_system_state()
        if st is None:
            self._logger.warning('Could not read system state (no response from CCU).')
            self._set_system_status('System: state unknown (no response)', SYSTEM_STATUS_COLOR_BUSY)
            return
        state = st['startup_state']
        name = SYSTEM_STARTUP_STATE_NAMES.get(state, f'UNKNOWN({state})')
        self._logger.info(f'Current system state: {name}.')
        self._set_system_status(f'System: {name}', self._system_state_color(state))
        if state == SYSTEM_STATE_IDLE:
            # Idle: nothing running, ready to start.
            self._system_status_timer.stop()
            self._system_watchdog_timer.stop()
            self._system_stalled = False
        elif state == SYSTEM_STATE_RUNNING:
            # Already running: data should be flowing -> passive watchdog, no polling.
            self._logger.info('System already RUNNING; monitoring data flow.')
            now = time.monotonic()
            for d in self.manager.devices.values():
                if d.last_data_time == 0.0:
                    d.last_data_time = now
            self._system_status_timer.stop()
            self._system_stalled = False
            self._system_watchdog_timer.start()
        elif state == SYSTEM_STATE_FAILED:
            self._system_status_timer.stop()
            self._system_watchdog_timer.stop()
            self._system_report_failure(dev, st)
        else:
            # Startup in progress (PINGING..STARTING_MEASUREMENT): follow until done.
            self._logger.info('System startup in progress; following until RUNNING/FAILED.')
            self._system_watchdog_timer.stop()
            self._system_stalled = False
            self._system_poll_count = 0
            self._system_status_timer.start()

    def _system_start(self):
        dev = self._system_ccu()
        if dev is None:
            self._set_system_status('System: no CCU', SYSTEM_STATUS_COLOR_ERROR)
            return
        if not dev.system_startup_start(0):
            self._logger.error(f'Start command not acknowledged by CCU {dev.ip}.')
            self._set_system_status('System: start failed (no ACK)', SYSTEM_STATUS_COLOR_ERROR)
            return
        self._logger.info(f'Startup sequence started on CCU {dev.ip}.')
        self._set_system_status('System: starting...', SYSTEM_STATUS_COLOR_BUSY)
        self._system_watchdog_timer.stop()
        self._system_stalled = False
        self._system_poll_count = 0
        self._system_status_timer.start()

    def _system_stop(self):
        self._system_status_timer.stop()
        self._system_watchdog_timer.stop()
        self._system_stalled = False
        dev = self._system_ccu()
        if dev is None:
            self._set_system_status('System: no CCU', SYSTEM_STATUS_COLOR_ERROR)
            return
        if dev.system_stop():
            self._logger.info(f'Stop command sent to CCU {dev.ip}.')
            self._set_system_status('System: stopped', SYSTEM_STATUS_COLOR_IDLE)
        else:
            self._logger.error(f'Stop command not acknowledged by CCU {dev.ip}.')
            self._set_system_status('System: stop failed (no ACK)', SYSTEM_STATUS_COLOR_ERROR)

    def _system_poll_status(self):
        """Poll the CCU startup state until RUNNING or FAILED (FW-style)."""
        self._system_poll_count += 1
        if self._system_poll_count > SYSTEM_STATUS_POLL_MAX:
            self._system_status_timer.stop()
            self._logger.warning('Timeout waiting for system startup to complete.')
            self._set_system_status('System: start timeout', SYSTEM_STATUS_COLOR_ERROR)
            return
        dev = self._system_ccu()
        if dev is None:
            self._system_status_timer.stop()
            self._set_system_status('System: no CCU', SYSTEM_STATUS_COLOR_ERROR)
            return
        st = dev.get_system_state()
        if st is None:
            self._set_system_status('System: starting... (no response)', SYSTEM_STATUS_COLOR_BUSY)
            return
        state = st['startup_state']
        name = SYSTEM_STARTUP_STATE_NAMES.get(state, f'UNKNOWN({state})')
        self._set_system_status(f'System: {name}', self._system_state_color(state))
        if state == SYSTEM_STATE_RUNNING:
            self._system_status_timer.stop()
            self._logger.info('System is RUNNING.')
            # Data should flow now; stop polling and switch to a passive data-flow
            # watchdog that only reads state by command if data stops.
            now = time.monotonic()
            for d in self.manager.devices.values():
                if d.last_data_time == 0.0:
                    d.last_data_time = now
            self._system_stalled = False
            self._system_watchdog_timer.start()
        elif state == SYSTEM_STATE_FAILED:
            self._system_status_timer.stop()
            self._system_report_failure(dev, st)

    def _system_report_failure(self, dev, st):
        err = st['startup_error']
        err_name = SYSTEM_STARTUP_ERROR_NAMES.get(err, f'UNKNOWN({err})')
        culprit = 'CCU' if st['err_node'] == 0 else f"Node {st['err_node']}"
        self._logger.error(f'System startup FAILED: {err_name} (culprit: {culprit}).')
        self._set_system_status(f'System: FAILED - {err_name} ({culprit})', SYSTEM_STATUS_COLOR_ERROR)
        if err == SYSTEM_ERROR_CLOCK_FREQ:
            val = st['err_value']
            if val == 0:
                self._logger.error(f'  {culprit}: 0 Hz - NO CLOCK SIGNAL detected.')
            else:
                self._logger.error(f'  {culprit}: measured {val} Hz '
                                   f'(expected {SYSTEM_CLOCK_FREQ_MIN}..{SYSTEM_CLOCK_FREQ_MAX}).')
            self._logger.error(f'  CCU local clock: {st["freq_hz"]} Hz.')
        # Per-device diagnostics: acquisition freq + PTP lock + packet count.
        self._system_log_device_diagnostics()

    def _system_log_device_diagnostics(self):
        for idx, (ip, dev) in enumerate(self.manager.devices.items()):
            acq = dev.get_acquisition_state()
            if acq is None:
                self._logger.warning(f'  [dev{idx} {ip}] no acquisition state (no response).')
                continue
            freq = acq['freq_hz']
            if freq == 0:
                verdict = '0 Hz (NO CLOCK)'
            elif freq < SYSTEM_CLOCK_FREQ_MIN:
                verdict = f'{freq} Hz (TOO LOW)'
            elif freq > SYSTEM_CLOCK_FREQ_MAX:
                verdict = f'{freq} Hz (TOO HIGH)'
            else:
                verdict = f'{freq} Hz (OK)'
            self._logger.info(f'  [dev{idx} {ip}] freq={verdict}, ptp_locked={acq["ptp_sync_locked"]}, '
                              f'packets={acq["packets"]}, flags=0x{acq["state_flags"]:04X}')

    def _system_watchdog(self):
        """After RUNNING: only read system state by command if data stops flowing."""
        if not self.manager.devices:
            return
        now = time.monotonic()
        stalled = []
        for idx, (ip, dev) in enumerate(self.manager.devices.items()):
            age = (now - dev.last_data_time) if dev.last_data_time else None
            if age is None or age > SYSTEM_DATA_STALL_TIMEOUT_S:
                stalled.append((idx, ip))
        if not stalled:
            if self._system_stalled:
                self._system_stalled = False
                self._logger.info('Data flow restored on all devices.')
                self._set_system_status('System: RUNNING', SYSTEM_STATUS_COLOR_OK)
                for ip in self.manager.devices:
                    self._set_device_status_color(ip, SYSTEM_STATUS_COLOR_OK)
            return
        if self._system_stalled:
            return  # already diagnosing this stall; wait for recovery
        self._system_stalled = True
        stalled_ips = [ip for _, ip in stalled]
        for ip in stalled_ips:
            self._set_device_status_color(ip, SYSTEM_STATUS_COLOR_BUSY)
        names = ', '.join(f'dev{idx} ({ip})' for idx, ip in stalled)
        self._logger.warning(f'Data stopped from: {names}. Diagnosing...')
        # FW-side view (informative): read the CCU system state.
        dev = self._system_ccu()
        st = dev.get_system_state() if dev is not None else None
        self._system_diag_keep_status = False
        if st is None:
            self._logger.error('  System state: no response from CCU.')
            self._system_stall_status_base = 'System: data stalled'
        else:
            state = st['startup_state']
            name = SYSTEM_STARTUP_STATE_NAMES.get(state, f'UNKNOWN({state})')
            self._system_stall_status_base = f'System: data stalled - state {name}'
            if state == SYSTEM_STATE_FAILED:
                self._system_report_failure(dev, st)
                self._system_diag_keep_status = True
            else:
                self._logger.warning(f'  System state: {name}, packets={st["packets"]}, '
                                     f'CCU clock={st["freq_hz"]} Hz.')
        if not self._system_diag_keep_status:
            self._set_system_status(f'{self._system_stall_status_base} (diagnosing...)', SYSTEM_STATUS_COLOR_ERROR)
        # Per-device receiver-registration + firewall diagnostic (async chain).
        self._system_diag_pending = set(stalled_ips)
        self._system_diag_firewall = False
        self._system_diag_unresponsive = False
        self._system_diagnose_stalled(stalled_ips)

    def _device_data_fresh(self, dev):
        """True if the device has produced data within the stall timeout."""
        return bool(dev.last_data_time) and (time.monotonic() - dev.last_data_time) <= SYSTEM_DATA_STALL_TIMEOUT_S

    def _set_device_status_color(self, ip, color):
        """Colour the top-left 'Device n' label whose IP matches, to signal its state."""
        for de, dl in zip(self.device_edits, self.device_labels):
            if de.text().strip().split(':')[0] == ip:
                dl.setStyleSheet(f'background-color: {color}' if color else '')
                break

    def _flash_device_trigger(self, ip):
        """Briefly flash the matching 'Device n' label blue when a trigger packet arrives."""
        for de, dl in zip(self.device_edits, self.device_labels):
            if de.text().strip().split(':')[0] != ip:
                continue
            timer = getattr(dl, '_trigger_timer', None)
            if timer is None:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda dl=dl: dl.setStyleSheet(getattr(dl, '_trigger_base_style', '')))
                dl._trigger_timer = timer
            if not timer.isActive():
                dl._trigger_base_style = dl.styleSheet()
            dl.setStyleSheet(TRIGGER_FLASH_STYLE)
            timer.start(TRIGGER_FLASH_MS)
            break

    def _system_diagnose_stalled(self, stalled_ips):
        """Diagnose devices that stopped sending data: receiver registration, then firewall."""
        for ip in stalled_ips:
            dev = self.manager.devices.get(ip)
            if dev is not None:
                self._system_diag_register(dev)

    def _system_diag_complete(self, dev):
        """Mark one device's diagnosis as done; when all finish, drop the (diagnosing...) tag."""
        self._system_diag_pending.discard(dev.ip)
        if self._system_diag_pending:
            return  # other devices are still being diagnosed
        if self._system_diag_keep_status:
            return  # a more specific status (e.g. FAILED) is already shown
        if self._system_diag_unresponsive:
            self._set_system_status('System: data stalled - device unresponsive', SYSTEM_STATUS_COLOR_ERROR)
        elif self._system_diag_firewall:
            self._set_system_status('System: data stalled - firewall blocking data socket', SYSTEM_STATUS_COLOR_ERROR)
        elif any(not self._device_data_fresh(d) for d in self.manager.devices.values()):
            self._set_system_status(self._system_stall_status_base, SYSTEM_STATUS_COLOR_ERROR)
        # else: data resumed on all devices; the watchdog will restore RUNNING on its next tick.

    def _system_diag_register(self, dev):
        # Step 1: check whether SCADA is registered as a data receiver on this device.
        try:
            addr, pr = self.receiver_edit.text().split(':')
            want = (addr, int(pr))
        except Exception:
            self._logger.warning(f'[{dev.ip}] Bad receiver address; cannot check registration.')
            QTimer(self).singleShot(0, lambda d=dev: self._system_diag_after_register(d))
            return
        recv = dev.get_receivers(RECEIVER_TYPE_DATA)
        if recv is None:
            # No reply to GET_RECEIVERS -> device is in an incorrect/unresponsive state.
            # Stop here: a data-socket firewall test makes no sense and firewall is not the main issue.
            self._logger.error(f'[{dev.ip}] GET_RECEIVERS failed -> device unresponsive/incorrect state; '
                               f'skipping firewall test (firewall not the likely cause).')
            self._system_diag_unresponsive = True
            self._set_device_status_color(dev.ip, SYSTEM_STATUS_COLOR_ERROR)
            self._system_diag_complete(dev)
            return
        elif want in recv['active']:
            self._logger.info(f'[{dev.ip}] Already registered as data receiver {want[0]}:{want[1]} '
                              f'(active={recv["active"]}); skipping re-registration.')
            self._system_diag_after_register(dev, was_registered=True)
            return
        else:
            self._logger.info(f'[{dev.ip}] Not registered as data receiver (active={recv["active"]}) '
                              f'-> registering {want[0]}:{want[1]}.')
            dev.register_receiver(*want)
        QTimer(self).singleShot(SYSTEM_DIAG_WAIT_MS, lambda d=dev: self._system_diag_after_register(d))

    def _system_diag_after_register(self, dev, was_registered=False):
        if self._device_data_fresh(dev):
            if not was_registered:
                self._logger.info(f'[{dev.ip}] Data resumed after registration -> was not registered as receiver.')
            else:
                self._logger.info(f'[{dev.ip}] Data resumed.')
            self._set_device_status_color(dev.ip, SYSTEM_STATUS_COLOR_OK)
            self._system_diag_complete(dev)
            return
        # Step 2: firewall test - ping out of the data socket (unless disabled).
        if FIREWALL_PENETRATION == 'off':
            self._logger.info(f'[{dev.ip}] Still no data; firewall penetration disabled (off) -> '
                              f'not testing the data socket, treating stall as a genuine/correct state.')
            self._set_device_status_color(dev.ip, SYSTEM_STATUS_COLOR_ERROR)
            self._system_diag_complete(dev)
            return
        silent = FIREWALL_PENETRATION != 'verbose'
        ping_kind = 'silent' if silent else 'verbose'
        self._logger.info(f'[{dev.ip}] Still no data -> {ping_kind} ping via data socket to data port {dev.data_port} (firewall test).')
        dev.last_ack_time = 0.0
        t0 = time.monotonic()
        dev.ping(self.manager.data_socket, silent=silent, port=dev.data_port)
        QTimer(self).singleShot(SYSTEM_DIAG_WAIT_MS, lambda d=dev, t=t0: self._system_diag_after_ping(d, t))

    def _system_diag_after_ping(self, dev, t0):
        if self._device_data_fresh(dev):
            self._logger.info(f'[{dev.ip}] Data resumed after firewall ping.')
            self._set_device_status_color(dev.ip, SYSTEM_STATUS_COLOR_OK)
        elif dev.last_ack_time >= t0:
            self._logger.info(f'[{dev.ip}] Data socket reachable (ping ACK received) but still no data '
                              f'-> stall is a genuine/correct state.')
            self._set_device_status_color(dev.ip, SYSTEM_STATUS_COLOR_ERROR)
        else:
            self._logger.error(f'[{dev.ip}] No ping ACK on data socket -> firewall is likely blocking '
                               f'incoming packets on the data socket.')
            self._system_diag_firewall = True
            self._set_device_status_color(dev.ip, SYSTEM_STATUS_COLOR_ERROR)
        self._system_diag_complete(dev)

    def _reset_counter(self):
        #self.manager.broadcast('reset_counter')
        self.manager.reset_counter()
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

    def _get_trigger_config(self):
        cfgs = self.manager.get_trigger_config_all()
        widgets = self.device_trigger_settings + self.device_trigger_holdoff
        for k in widgets:
            k.blockSignals(True)
        for ip, cfg in cfgs.items():
            if cfg is None:
                self._logger.warning(f"Trigger config for {ip}: not available")
                continue
            config_byte, holdoff_ns = cfg
            for i, edit in enumerate(self.device_edits):
                line_ip = edit.text().strip().split(':')[0]
                if line_ip == ip:
                    self.device_trigger_settings[i].setCurrentIndex(trigger_byte_to_index(config_byte))
                    self.device_trigger_holdoff[i].setValue(int(holdoff_ns // 1000))
                    break
            self._logger.info(f"Trigger config for {ip}: byte={config_byte:02X}, holdoff={holdoff_ns} ns")
        for k in widgets:
            k.blockSignals(False)

    def _update_trigger_settings(self, row: int):
        """Compose trigger config from combo+holdoff and apply (RAM only)."""
        txt = self.device_edits[row].text().strip()
        if not txt:
            self._logger.warning(f'[TriggerCtrl] Row {row}: no IP set')
            return
        ip = txt.split(':')[0]
        dev = self.manager.devices.get(ip)
        if not dev:
            self._logger.warning(f'[TriggerCtrl] {ip}: device not applied yet')
            return
        index = self.device_trigger_settings[row].currentIndex()
        config_byte = trigger_index_to_byte(index)
        holdoff_ns = self.device_trigger_holdoff[row].value() * 1000
        dev.set_trigger_config(config_byte=config_byte, holdoff_ns=holdoff_ns, save=False)
        self._logger.info(
            f'[TriggerCtrl] {ip}: {TRIGGER_SETTINGS[index]} ({config_byte:02X}), holdoff={holdoff_ns} ns command sent'
        )

    def _save_trigger_config(self):
        reply = QMessageBox.question(self, "Save?",
                                    "Do you really want to save trigger configuration to EEPROM?",
                                    QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for row in range(DeviceManager.MAX_DEVICES):
            if not self.device_checks[row].isChecked():
                continue
            ip = self.device_edits[row].text().strip().split(':')[0]
            dev = self.manager.devices.get(ip)
            if dev is not None:
                config_byte = trigger_index_to_byte(self.device_trigger_settings[row].currentIndex())
                holdoff_ns = self.device_trigger_holdoff[row].value() * 1000
                dev.set_trigger_config(config_byte=config_byte, holdoff_ns=holdoff_ns, save=True)

    def clear_plot(self):
        for dev in self.manager.devices.values():
            with dev.buffer.lock:
                dev.buffer.time.clear()
                for dq in dev.buffer.signal: dq.clear()
                for dq in dev.buffer.error: dq.clear()
                dev.buffer.ptp.clear()
                dev.buffer.result_fault_state.clear()
                dev.buffer.result_parity_errors.clear()
                dev.buffer.result_crc_error_mask.clear()
        self.ax.clear()
        self.curves.clear()

        #if hasattr(self, 'ax_result'):
        self.ax_result.clear()
        self.ax_result_curves.clear()

        self._update_plot(force=True)
        self._logger.info('Graf cleaned')
    
    def save_measurement(self):
        try:
            measurement_nr = int(self.measurement_number_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Saving error", "Measurement number is invalid.")
            return

        try:
            dir_path = 'RICE_mereni'
            os.makedirs(dir_path, exist_ok=True)

            file_name = os.path.join(dir_path, str(measurement_nr).zfill(4))
            files = self.save_data(file_prefix=file_name, strict=True)

        except Exception as e:
            self._logger.exception("Measurement save failed")
            QMessageBox.critical(
                self,
                "Measurement was not saved correctly",
                f"Saving failed:\n{e}"
            )
            return

        measurement_nr += 1
        self.measurement_number_edit.setText(str(measurement_nr))

        #QMessageBox.information(
        #    self,
        #    "Measurement saved",
        #    "Saved files:\n" + "\n".join(files)
        #)

    def save_data(self, file_prefix=None, strict=True):
        if not file_prefix:
            path, _ = QFileDialog.getSaveFileName(self, 'Save Data', '', 'CSV Files (*.csv)')
            if not path:
                return
            base = path.rstrip('.csv')
        else:
            base = file_prefix

        files = []

        if not self.manager.devices:
            raise RuntimeError("Nejsou aplikovaná žádná zařízení.")

        for idx, (ip, dev) in enumerate(self.manager.devices.items()):
            fname = f"{base}_dev{idx}.csv"

            with dev.buffer.lock:
                # Read the per-sample columns as numpy arrays (one contiguous copy)
                # instead of materialising python lists up front; the expensive
                # per-element python conversion is deferred until after the window
                # trim below, so only the kept rows are ever turned into python ints.
                times_a = np.asarray(dev.buffer.time)
                ptp_a = np.asarray(dev.buffer.ptp)
                signals_a = [np.asarray(dev.buffer.signal[c + 1]) for c in range(dev.channels)]
                result_fault_state = list(dev.buffer.result_fault_state)
                result_parity_errors = list(dev.buffer.result_parity_errors)
                result_crc_error_mask = list(dev.buffer.result_crc_error_mask)

            # CCU devices carry per-result-packet metadata; nodes leave these empty.
            has_result_meta = bool(result_crc_error_mask)

            # Node buffers store one sample per row (5 us step); the CCU result buffer
            # stores one result packet per row (1 ms step).
            time_scale = PACKET_PERIOD if has_result_meta else SAMPLING_PERIOD

            # Align CSV time so the trigger sample is at t = 0 (matches the plot).
            _tsi = dev.trigger_sample_index()
            time_zero_s = _tsi * SAMPLING_PERIOD if _tsi is not None else 0.0
            # Same window trim as the plot: keep exactly pre+post packets. Trim by
            # integer sample/packet number relative to the trigger so every device
            # yields the same count regardless of PTP float rounding.
            _pre = dev.pretrigger_packets
            _post = ptp_mode.samples_awaited
            _trim_window = _tsi is not None and _post > 0
            if has_result_meta:
                _trig_n = _tsi / SAMPLES_PER_PACKET
                _n_lo, _n_hi = _trig_n - _pre, _trig_n + _post
                # Result rows sit on the 1 ms packet grid; zero on the trigger packet
                # so times stay whole ms (the trigger's intra-packet offset is dropped).
                time_zero_s = round(_trig_n) * PACKET_PERIOD
            else:
                _n_lo, _n_hi = _tsi - _pre * SAMPLES_PER_PACKET, _tsi + _post * SAMPLES_PER_PACKET

            if strict and len(times_a) == 0:
                raise RuntimeError(f"Zařízení {ip} nemá žádná data k uložení.")

            lengths = [len(times_a), len(ptp_a), *(len(s) for s in signals_a)]
            if has_result_meta:
                lengths += [len(result_fault_state), len(result_parity_errors), len(result_crc_error_mask)]
            row_count = min(lengths)

            if strict and row_count == 0:
                raise RuntimeError(f"Zařízení {ip} má prázdný buffer.")

            tmp_name = fname + ".tmp"

            # Truncate every column to the common row count, then apply the same
            # window trim as the plot in a single vectorised pass over numpy arrays.
            times_a = times_a[:row_count]
            ptp_a = ptp_a[:row_count]
            signals_a = [s[:row_count] for s in signals_a]
            if _trim_window:
                keep = (times_a >= _n_lo) & (times_a < _n_hi)
            else:
                keep = np.ones(row_count, dtype=bool)
            t_shift_a = times_a * time_scale - time_zero_s

            with open(tmp_name, 'w', newline='') as f:
                w = csv.writer(f)
                header = ['time', 'ptp_ns'] + [f'ch{c}' for c in range(dev.channels)]
                if has_result_meta:
                    n_fault = GATHERING_DEVICES
                    n_parity = GATHERING_DEVICES * ACQUISITION_CHANNELS
                    header += [f'fault_state{d}' for d in range(n_fault)]
                    header += [f'parity_n{p // ACQUISITION_CHANNELS}c{p % ACQUISITION_CHANNELS}' for p in range(n_parity)]
                    header += ['crc_error_mask']
                w.writerow(header)

                if has_result_meta:
                    # Result rows carry per-packet metadata tuples (few rows, 1 ms
                    # grid): expand them in a small python loop over the kept indices.
                    # The time column uses the same fixed 6-decimal format as the
                    # node path so all saved CSVs read with an aligned time column.
                    t_shift_list = t_shift_a.tolist()
                    ptp_list = ptp_a.tolist()
                    sig_lists = [s.tolist() for s in signals_a]
                    rows = []
                    for i in np.nonzero(keep)[0].tolist():
                        row = [sig_lists[ch][i] for ch in range(dev.channels)]
                        row += list(result_fault_state[i])
                        row += list(result_parity_errors[i])
                        row += [result_crc_error_mask[i]]
                        rows.append(['%.6f' % t_shift_list[i], ptp_list[i], *row])
                    w.writerows(rows)
                else:
                    # Node path (the large 2.2M-row case): format each row with a
                    # single % template and bulk-write -- ~1.6x faster than
                    # csv.writer. The time column uses a fixed 6-decimal format
                    # (microsecond resolution; the sample step is 5 us) so the
                    # column stays aligned and easy to read by hand. csv's default
                    # line terminator is '\r\n', matched here.
                    t_shift_list = t_shift_a[keep].tolist()
                    ptp_list = ptp_a[keep].tolist()
                    sig_lists = [s[keep].tolist() for s in signals_a]
                    if t_shift_list:
                        tmpl = '%.6f,' + ','.join(['%d'] * (dev.channels + 1))
                        lines = [tmpl % row for row in zip(t_shift_list, ptp_list, *sig_lists)]
                        f.write('\r\n'.join(lines))
                        f.write('\r\n')

                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_name, fname)
            files.append(fname)

            if strict and os.path.getsize(fname) == 0:
                raise RuntimeError(f"Soubor {fname} je prázdný.")

        #png_data = f"{base}_data.png"
        #exporter_data = pyqtgraph.exporters.ImageExporter(self.ax)
        #exporter_data.export(png_data)

        png_name = f"{base}.png"
        # Export celé GUI části s oběma grafy pod sebou
        pixmap = self.plot_widget.grab()
        if not pixmap.save(png_name, "PNG"):
            raise RuntimeError(f"Nepodařilo se uložit obrázek {png_name}.")

        if strict:
            if not os.path.exists(png_name):
                raise RuntimeError(f"Nepodařilo se vytvořit obrázek {png_name}.")
            if os.path.getsize(png_name) == 0:
                raise RuntimeError(f"Obrázek {png_name} je prázdný.")
        
        files.append(png_name)

        self._logger.info('Saved data: ' + ', '.join(files))
        return files
        
    def _update_plot(self, force=False):
        # Skip the (expensive) full redraw when no device buffer has changed since the
        # last frame -- e.g. a trigger / "start new sampling" capture is complete and
        # already holds all the data it asked for, even though the system keeps running
        # and sending. Every append bumps buffer.revision; a changed set of devices or
        # force=True (used after clear_plot) also redraws.
        revisions = {ip: dev.buffer.revision for ip, dev in self.manager.devices.items()}
        if not force and self._plot_revisions == revisions:
            return
        self._plot_revisions = revisions
        lines = []
        received_counts = []
        #for (ip, dev) in self.manager.devices.items():
        for dev_index, (ip, dev) in enumerate(self.manager.devices.items()):
            buf = dev.buffer
            if not buf.time:
                continue
            with buf.lock:
                # Data processing
                if (dev_index != CCU_DEVICE_INDEX):
                    idx = np.array(buf.signal[0], dtype=float)
                    tsi = dev.trigger_sample_index()
                    if tsi is not None:
                        # Shift so the trigger sample sits at t = 0 (per-sample, on
                        # the unified buffer rather than per packet).
                        idx -= tsi
                    # Stable-sort the per-sample buffer by time so a late/replayed
                    # packet around the trigger cannot draw a zig-zag at the pre/post
                    # boundary; then drop samples before the pre-trigger window.
                    order_perm = np.argsort(idx, kind='stable')
                    idx = idx[order_perm]
                    if tsi is not None:
                        trim = int(np.searchsorted(idx, -dev.pretrigger_packets * SAMPLES_PER_PACKET, side='left'))
                    else:
                        trim = 0
                    # Cut the tail of the extra packet so exactly the requested
                    # post-trigger window is shown (samples_awaited packets).
                    if tsi is not None and ptp_mode.samples_awaited > 0:
                        trim_end = int(np.searchsorted(idx, ptp_mode.samples_awaited * SAMPLES_PER_PACKET, side='left'))
                    else:
                        trim_end = len(idx)
                    received_full = int(len(idx) // SAMPLES_PER_PACKET)
                    x = (idx * SAMPLING_PERIOD)[trim:trim_end]
                    avgs = [0] * dev.channels
                    for ch in range(dev.channels):
                        key = (ip, ch)
                        if key not in self.curves:
                            self.curves[key] = self.ax.plot(pen=Plotter.Colors[len(self.curves)], name=f'{ip}[{ch}]')

                        #y = np.array(buf.signal[ch + 1])[-len(x):]
                        raw = np.array(buf.signal[ch + 1], dtype=float)[order_perm][trim:trim_end]
                        # Kalibrace z ID paketu
                        gain = 1.0
                        offset = 0.0
                        unit = ""
                        if hasattr(dev, "info"):
                            try:
                                gain = dev.info["channels"][ch]["gain"]
                                offset = dev.info["channels"][ch]["offset"]
                                unit_raw = dev.info["channels"][ch]["unit"]
                                if isinstance(unit_raw, bytes):
                                    unit = unit_raw.decode("ascii", errors="ignore").rstrip("\0").strip()
                                else:
                                    unit = str(unit_raw)
                            except Exception:
                                pass
                        # Přepočet pouze pro zobrazení
                        y = raw * gain + offset
                        if key not in self.curves:
                            pen = pg.mkPen(Plotter.Colors[len(self.curves)], width=2)
                            curve_name = f'{ip}[{ch}]'
                            if unit:
                                curve_name += f' [{unit}]'
                            self.curves[key] = self.ax.plot(pen=pen, name=curve_name)

                        avgs[ch] = np.mean(y[-min(len(y), SAMPLES_PER_PACKET * DEFAULT_AVG_LEN_MS):])
                        self.curves[key].setData(x[-len(y):], y)
                
                    # Error calculation
                    errs = ','.join(str(sum(list(buf.error[c])[-SAMPLES_PER_PACKET:])) for c in range(dev.channels))

                    # Statistics part
                    received = received_full
                    # Count only packets inside the requested window so a missing
                    # packet anywhere (start, middle, end) shows up as a shortfall.
                    if tsi is not None and ptp_mode.samples_awaited > 0:
                        received = int((trim_end - trim) // SAMPLES_PER_PACKET)

                # Result processing
                else:
                    x = np.array(buf.signal[0]) * PACKET_PERIOD
                    # Statistics part
                    received = int(len(x))
                    # interpolete x to stretch graph to the same width as signal plot, so each packet corresponds to SAMPLES_PER_PACKET samples on the graph 
                    n = x.size
                    if n < 2:
                        received = n
                        avgs = [0]
                        errs = ','.join('0' for _ in range(dev.channels))
                        continue

                    x_pkt = np.array(buf.signal[0], dtype=float) * PACKET_PERIOD
                    received = int(len(x_pkt))
                    avgs = [0]
                    if received == 0:
                        continue
                    # create 200 samples per 1 ms packet interval:
                    # packet result at t=0 is valid on <0 ms, 1 ms>
                    x = (
                        np.repeat(x_pkt, SAMPLES_PER_PACKET)
                        + np.tile(np.arange(SAMPLES_PER_PACKET), received) * SAMPLING_PERIOD
                    )

                    tsi = dev.trigger_sample_index()
                    if tsi is not None:
                        # Same per-sample time-zero shift + pre-trigger window trim
                        # as the node path (tsi is in sample units). Packet-align the
                        # zero (drop trigger's intra-packet offset) so result rows stay
                        # on the whole-ms grid, matching the saved CSV.
                        x = x - round(tsi / SAMPLES_PER_PACKET) * PACKET_PERIOD
                        trim = int(np.searchsorted(x, -dev.pretrigger_packets * PACKET_PERIOD, side='left'))
                        # Cut the tail of the extra packet to exactly post packets;
                        # include the sample that lands exactly on the window edge.
                        if ptp_mode.samples_awaited > 0:
                            trim_end = int(np.searchsorted(x, ptp_mode.samples_awaited * PACKET_PERIOD, side='right'))
                        else:
                            trim_end = x.size
                    else:
                        trim = 0
                        trim_end = x.size
                    x = x[trim:trim_end]
                    # Count only result packets inside the window so a missing one
                    # shows up as a shortfall (matches the node statistics).
                    if tsi is not None and ptp_mode.samples_awaited > 0:
                        received = int((trim_end - trim) // SAMPLES_PER_PACKET)

                    avgs = [0]  # Dummy for uniform output
                    center = (dev.channels - 1) / 2.0
                    offset_step = 0.05  # vertical spacing between bit lines
                    # Use first data channel (signal[1]) as byte source
                    #y_src = np.array(buf.signal[1])[-len(x):]
                    # Convert to unsigned bytes; mask to 16 bits
                    #byte_values = y_src.astype(np.int32) & 0xFFFF
                    for bit_idx in range(dev.channels):
                        if bit_idx not in self.ax_result_curves:
                            color = Plotter.Colors[len(self.ax_result_curves) % len(Plotter.Colors)]
                            self.ax_result_curves[bit_idx] = self.ax_result.plot(pen=color, name=f'bit{bit_idx}')
                            #self.ax_result_curves[bit_idx] = self.ax_result.step(pen=color, where='post', name=f'bit{bit_idx}')

                        #bit_vals = ((byte_values >> bit_idx) & 1).astype(float)
                        y = np.array(buf.signal[bit_idx + 1], dtype=float)
                        y = np.repeat(y, SAMPLES_PER_PACKET) # Y interpolation - steps
                        # Slight offset so bits with same logical value are still visible
                        offset = (bit_idx - center) * offset_step
                        y_bits = (y + offset)[trim:trim_end]

                        self.ax_result_curves[bit_idx].setData(x[-len(y_bits):], y_bits)
                        #self.ax_result.step(x[-len(y_bits):], y_bits, where='post', linewidth=2)
                    
                    # Error calculation
                    errs = ','.join(str(sum(list(buf.error[c])[-1:])) for c in range(dev.channels))

            # Statistics
            received_counts.append(received)
            avgs = ', '.join(map(lambda v: f'{v:.3f}', avgs))
            sent = self.last_order.get(ip)
            if sent is None:
                sent = 0
            else:
                sent += 1
            #received = int(len(x)//SAMPLES_PER_PACKET)
            sent = max(sent, received) # sent is updated in data_ready signal, which can be delayed from receiving buffer on heavy load
            stat_line = f'{ip}: packets = {received}/{sent}/{self.expected_samples}; errs = {errs}; avg = {avgs}'

            if (received == dev.received_last):
                if received < self.expected_samples:
                    stat_line = f'<span style="color:red;">{stat_line}</span>'
            lines.append(stat_line)
            dev.received_last = received

        self.error_lbl.setText(f'Statistic (ip: received / sent / expected packets (ms); channels parity errors; channels average per {DEFAULT_AVG_LEN_MS} ms):<br>' + 
                               "<br>".join(lines))
        self.error_lbl.setTextFormat(Qt.RichText)
        self._clear_sampling_indicator_if_complete(received_counts)
    
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
        init_fns = (self._ping_all, self._register_logger_all, self._get_ids, self._get_clock_config, self._get_trigger_config, self._register_all, self._register_ccu, self._reset_counter)
        for i, f in enumerate(init_fns):
            QTimer(self).singleShot(i * 100, f)
        # After init, query the current system state, display it and react to it.
        QTimer(self).singleShot(len(init_fns) * 100, self._system_refresh_state)

def main(argv):
    with ExitStack() as stack:
        stack.enter_context(logging_:=logger.Logging())

        try:
            import default_settings as ds
        except ImportError:
            ds = None

        global PTP_TRIGGER_RING_PACKETS
        global FIREWALL_PENETRATION
        global DATA_SOCKET_RCVBUF_BYTES
        global TRIGGER_CAPTURE_MARGIN_PACKETS
        global BUFFER_LENGTH_S, BUFFER_SIZE, MAX_CAPTURE_PACKETS

        DEFAULT_FIRST_IP = getattr(ds, 'DEFAULT_FIRST_IP', "192.168.137.100")
        DEFAULT_LEADER = getattr(ds, 'DEFAULT_LEADER', 1)
        DEVICES_COUNT = getattr(ds, 'DEVICES_COUNT', 5)
        DEFAULT_AVG_LEN_MS = getattr(ds, 'DEFAULT_AVG_LEN_MS', 1000)
        DEFAULT_PRETRIGGER_PACKETS = getattr(ds, 'DEFAULT_PRETRIGGER_PACKETS', 0)
        # Fall back to DEFAULT_AVG_LEN_MS so behaviour is unchanged when the setting/file is absent.
        DEFAULT_POSTTRIGGER_PACKETS = getattr(ds, 'DEFAULT_POSTTRIGGER_PACKETS', DEFAULT_AVG_LEN_MS)
        PTP_TRIGGER_RING_PACKETS = getattr(ds, 'PTP_TRIGGER_RING_PACKETS', PTP_TRIGGER_RING_PACKETS)
        TRIGGER_CAPTURE_MARGIN_PACKETS = int(getattr(ds, 'TRIGGER_CAPTURE_MARGIN_PACKETS', TRIGGER_CAPTURE_MARGIN_PACKETS))
        # Sample-buffer length: recompute the derived sizes (used by DeviceBuffer ring
        # allocation and the post-trigger spinbox) before any device/GUI is created.
        BUFFER_LENGTH_S = int(getattr(ds, 'BUFFER_LENGTH_S', BUFFER_LENGTH_S))
        BUFFER_SIZE = int(BUFFER_LENGTH_S * SAMPLES_PER_PACKET * PACKET_RATE_HZ)
        MAX_CAPTURE_PACKETS = BUFFER_SIZE // SAMPLES_PER_PACKET
        ptp_mode.enabled = getattr(ds, 'DEFAULT_PTP_MODE_ENABLED', False)
        SOCKET_BACKEND = getattr(ds, 'SOCKET_BACKEND', DEFAULT_SOCKET_BACKEND)
        DATA_SOCKET_RCVBUF_BYTES = int(getattr(ds, 'DATA_SOCKET_RCVBUF_BYTES', DATA_SOCKET_RCVBUF_BYTES))
        FIREWALL_PENETRATION = str(getattr(ds, 'FIREWALL_PENETRATION', FIREWALL_PENETRATION)).lower()
        if FIREWALL_PENETRATION not in FIREWALL_PENETRATION_MODES:
            logging_.logger.warning(f"Invalid FIREWALL_PENETRATION={FIREWALL_PENETRATION!r}; falling back to 'on'. "
                                    f"Valid options: {FIREWALL_PENETRATION_MODES}.")
            FIREWALL_PENETRATION = 'on'

        if sys.platform.startswith('win'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling,True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,True)
        app=QApplication(argv)
        manager=DeviceManager(socket_backend=SOCKET_BACKEND, data_rcvbuf=DATA_SOCKET_RCVBUF_BYTES)
        gui=Plotter(manager)
        gui_log_handler = logger.CallbackHandler(sink_text=gui.log_signal.emit)
        gui_log_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d\t%(levelname)-8s\t%(name)-10s\t%(message)s"))
        gui_log_handler.formatter.datefmt='%H:%M:%S'
        gui_log_handler.setLevel(logging.DEBUG)
        logging_.log_printer.add_handler(gui_log_handler)
        logging_.logger.critical(f"Logging to file: {logging_.log_path}") # This has to be in console, so critical
        logging_.logger.info(f"Application started with settings: DEFAULT_FIRST_IP={DEFAULT_FIRST_IP}, DEFAULT_LEADER={DEFAULT_LEADER}, DEVICES_COUNT={DEVICES_COUNT}, DEFAULT_AVG_LEN_MS={DEFAULT_AVG_LEN_MS}, DEFAULT_PRETRIGGER_PACKETS={DEFAULT_PRETRIGGER_PACKETS}, DEFAULT_POSTTRIGGER_PACKETS={DEFAULT_POSTTRIGGER_PACKETS}, PTP_TRIGGER_RING_PACKETS={PTP_TRIGGER_RING_PACKETS}, DEFAULT_PTP_MODE_ENABLED={ptp_mode.enabled}, SOCKET_BACKEND={SOCKET_BACKEND}, DATA_SOCKET_RCVBUF_BYTES={DATA_SOCKET_RCVBUF_BYTES}, TRIGGER_CAPTURE_MARGIN_PACKETS={TRIGGER_CAPTURE_MARGIN_PACKETS}, BUFFER_LENGTH_S={BUFFER_LENGTH_S}, FIREWALL_PENETRATION={FIREWALL_PENETRATION}")
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
            gui.sample_spin.setValue(int(DEFAULT_POSTTRIGGER_PACKETS))
            gui.pretrigger_spin.setValue(int(DEFAULT_PRETRIGGER_PACKETS))
        QTimer(gui).singleShot(500, autoinit)
        #gui.show()
        gui.showMaximized()
        try:
            return app.exec_()
        finally:
            manager.shutdown()

if __name__=='__main__':
    sys.exit(main(sys.argv))

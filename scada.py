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
    DIGITAL_CHANNEL_FLAG_CURRENT, DIGITAL_CHANNEL_FLAG_LATCHED,
    UDP_CMD_PORT, UDP_DATA_PORT,
)
from fdds.crc import crc16_ccitt

APPLICATION_NAME = 'Eaton FDDS SCADA'
APPLICATION_VERSION = '1.14.3'
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
# Plot re-sorting overlap. The per-sample time-index column is appended in packet
# ARRIVAL order, so UDP reordering leaves only LOCAL inversions near where packets
# joined. The buffer is append-only within a capture, so each frame we only re-check
# (and, if needed, re-sort) the newly arrived tail plus this much overlap before it,
# instead of argsort-ing the whole multi-million-sample buffer every frame. Must be
# >= the worst-case packet reordering distance; 100 ms (=100 packets) is generous.
PLOT_SORT_MARGIN_SAMPLES = max(SAMPLES_PER_PACKET, int(round(0.1 / SAMPLING_PERIOD)))
DEFAULT_AVG_LEN_MS = 1000 # could be overwritten by default_settings.py
# GUI refresh interval in ms: how often the plot, fault indicators and analog value
# labels are redrawn. 100 ms = 10 Hz. Overridable via default_settings.py.
GUI_REFRESH_INTERVAL_MS = 100
CCU_DEVICE_INDEX   = 0
# ISOMON: an additional device type. It sits directly below Node 4 with the next
# consecutive IP. SCADA registers itself as its data/log receiver during init, but the
# CCU is NOT registered on it and the CCU is not told about it (see _register_ccu).
ISOMON_DEVICE_INDEX = 5
# GUI row labels: index 0 = CCU, 1..4 = Node 1..4, 5 = ISOMON.
DEVICE_LABELS = ['CCU', 'Node 1', 'Node 2', 'Node 3', 'Node 4', 'ISOMON']
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
# Device logs (PACKET_LOG) are delivered to SCADA's data socket, but the firmware sends
# them FROM the device's COMMAND port (cmd_upcb), not its data port. A stateful firewall
# keys the pinhole on both endpoints' ports, so the data stream's pinhole (data socket
# <-> device data port) does NOT cover logs (data socket <-> device command port). SCADA
# therefore keeps a separate pinhole open for logs by sending a ping out of the data
# socket to the device command port whenever a device has been silent (no data/log/ACK)
# for this long; the firewall closes an idle pinhole after ~this timeout and a log may
# not arrive that often. Gated by FIREWALL_PENETRATION ('off' disables it entirely;
# 'verbose' makes the ping non-silent so it shows up in the device log). Set at or
# slightly below the real firewall idle timeout. Overridable via default_settings.py.
FIREWALL_KEEPALIVE_TIMEOUT_S = 25

# Render plot curves with OpenGL (GPU). Off by default (CPU painter); can be much
# faster for very large traces but needs a working OpenGL driver. Overridable via
# default_settings.py (USE_OPENGL).
USE_OPENGL = False

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

FAULT_INDICATOR_SIZE = 20
FAULT_INDICATOR_COLORS = {
    (0, 0): '#2e7d32',
    (0, 1): '#f4d03f',
    (1, 0): '#e67e22',
    (1, 1): '#c0392b',
}
FAULT_INDICATOR_UNKNOWN = '#bdbdbd'
ANALOG_VALUE_FONT_STYLE = 'font-family: Consolas, "Courier New", monospace; font-size: 16px; font-weight: 600;'
# Muted style for analog labels with no live data (shows the '--' placeholder greyed
# out so a stalled channel is not mistaken for a valid last value).
ANALOG_VALUE_NO_DATA_STYLE = ANALOG_VALUE_FONT_STYLE + ' color: #9e9e9e;'

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
ID_HEADER_STRUCT = STRUCT.ID_V4         # <HH HBB HBBI3I HBBI HH (last HH = channels_count + _reserved)
ID_HEADER_STRUCT_V5 = STRUCT.ID_V5      # <HH HBB HBBI3I HBBI HBB (channels_count + fault counts)
CHANNEL_HEADER_STRUCT = STRUCT.CHANNEL  # <4s ff (unit, offset, gain)
DATA_HEADER_STRUCT = STRUCT.DATA_HEADER # <HHII (packet_type, packet_num, ptp_seconds, ptp_nanoseconds)
TRIGGER_PACKET_STRUCT = struct.Struct("<HHB3xII") # packet_type, packet_num, sample_num, ptp_seconds, ptp_nanoseconds

# Precomputed vectors for vectorised DATA-packet parsing (Phase 2 fast path).
_DATA_SAMPLE_INDEX = np.arange(SAMPLES_PER_PACKET, dtype=np.int64)
# Per-sample PTP step back from the packet's last-sample timestamp.
_DATA_PTP_BACK_STEPS = np.arange(SAMPLES_PER_PACKET - 1, -1, -1, dtype=np.int64) * NS_PER_SAMPLE

def _uses_v5_packet_format(info: dict | None) -> bool:
    return bool(info and info.get('fw_ver_major', 0) >= 5)

def _decode_c_string(raw) -> str:
    """Decode a fixed-size C string (NUL-terminated) to a Python str.

    Only the bytes before the first NUL are valid; the field's remaining bytes
    are padding. Non-bytes input is returned stringified/stripped unchanged.
    """
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).split(b'\x00', 1)[0].decode('ascii', errors='ignore').strip()
    return str(raw).strip()

def parse_id_packet(data):
    if len(data) < ID_HEADER_STRUCT.size:
        raise ValueError("[ERR]: ID packet is short")
    old_fields = ('packet_type',
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
    unpacked = ID_HEADER_STRUCT.unpack(data[:ID_HEADER_STRUCT.size])
    info = dict(zip(old_fields, unpacked))
    if _uses_v5_packet_format(info):
        v5_fields = old_fields[:-1] + ('fault_state_count', 'fault_latched_count')
        unpacked = ID_HEADER_STRUCT_V5.unpack(data[:ID_HEADER_STRUCT_V5.size])
        info = dict(zip(v5_fields, unpacked))
    else:
        info['fault_state_count'] = info['channels_count']
        info['fault_latched_count'] = 0
    channels_info = ('unit', 'offset', 'gain')
    info['cpu_uid'] = (info.pop('cpu_uid0'), info.pop('cpu_uid1'), info.pop('cpu_uid2'))
    # channels_count may exceed the number of channel_info entries the device
    # actually ships (e.g. the CCU reports fault_output_get_count() but only
    # carries ACQUISITION_CHANNELS channel_info slots), so only parse the
    # entries that are present to avoid reading past the packet.
    available_channels = (len(data) - ID_HEADER_STRUCT.size) // CHANNEL_HEADER_STRUCT.size
    parsed_channels = max(0, min(info['channels_count'], available_channels))
    info['channels'] = [dict(zip(channels_info, CHANNEL_HEADER_STRUCT.unpack(data[ID_HEADER_STRUCT.size+i*CHANNEL_HEADER_STRUCT.size:ID_HEADER_STRUCT.size+(i+1)*CHANNEL_HEADER_STRUCT.size]))) for i in range(parsed_channels)]
    # Decode each channel's fixed 4-byte unit C string once, here, so the rest of
    # the app can treat info['channels'][i]['unit'] as a ready Python str.
    for ch in info['channels']:
        ch['unit'] = _decode_c_string(ch.get('unit', b''))
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

    def tail(self, k:int):
        """Last ``k`` elements in arrival order (fewer if the ring holds < k), as a
        contiguous array. O(k) -- avoids materialising the whole buffer the way
        ``np.asarray(ring)`` / ``list(ring)`` do, for cheap 'recent samples' stats."""
        if k <= 0 or self._count == 0:
            return np.empty(0, dtype=self._dtype)
        k = min(int(k), self._count)
        start = (self._start + self._count - k) % self._cap
        end = start + k
        if end <= self._cap:
            return self._buf[start:end].copy()
        return np.concatenate((self._buf[start:], self._buf[:end - self._cap]))

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
        self.result_fault_latched  = deque(maxlen=BUFFER_SIZE)  # tuple per row: per-node fault_latched[GATHERING_DEVICES]
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
                      fault_state:Tuple[int, ...], fault_latched:Tuple[int, ...],
                      parity_errors:Tuple[int, ...], crc_error_mask:int):
        with self.lock:
            self.time.extend(t)
            for ch, sig in enumerate(samples):
                self.signal[ch+1].extend(sig)
                self.error[ch].extend_const(errs[ch], len(sig))
            self.signal[0].extend(t)
            self.ptp.extend(ptp)
            self.result_fault_state.extend([fault_state]*len(t))
            self.result_fault_latched.extend([fault_latched]*len(t))
            self.result_parity_errors.extend([parity_errors]*len(t))
            self.result_crc_error_mask.extend([crc_error_mask]*len(t))
            self.revision += 1


def _is_sorted(a) -> bool:
    """True if the 1-D array is non-decreasing (a[i] <= a[i+1] for all i)."""
    return a.size < 2 or bool(np.all(a[1:] >= a[:-1]))


def plot_sort_order(idx0, state, margin=PLOT_SORT_MARGIN_SAMPLES):
    """Stable argsort of an append-only, locally-reordered index column, computed
    incrementally so each call only sorts the newly appended tail plus ``margin``
    samples of overlap (the junction) instead of the whole buffer.

    ``idx0`` is the current arrival-order index array (1-D, integer). ``state`` is
    the dict returned by the previous call for the same device (or ``None`` on the
    first call / after a reset). Returns ``(order, new_state)`` where ``order`` is
    either:
      * ``None`` -- ``idx0`` is already ascending, so the identity order applies and
        the caller can skip reordering entirely (the common case), or
      * an int ``ndarray`` permutation mapping sorted position -> raw arrival index,
        byte-for-byte equal to ``np.argsort(idx0, kind='stable')`` as long as no
        packet was reordered by more than ``margin`` samples.

    Correctness relies on the column being append-only (detected via endpoint
    sentinels); any non-append change (clear, ring wrap, shrink) transparently
    falls back to a full sort.
    """
    n = int(idx0.size)
    if n == 0:
        return None, {'n': 0, 'first': 0, 'last': 0, 'order': None}

    append_ok = (state is not None and state['n'] > 0 and n >= state['n']
                 and idx0[0] == state['first'] and idx0[state['n'] - 1] == state['last'])

    def _state(order):
        return {'n': n, 'first': int(idx0[0]), 'last': int(idx0[n - 1]), 'order': order}

    if not append_ok:
        order = None if _is_sorted(idx0) else np.argsort(idx0, kind='stable')
        return order, _state(order)

    prev_n = state['n']
    if n == prev_n:                      # nothing appended since last frame
        return state['order'], state
    prev_order = state['order']
    lo = max(0, prev_n - margin)         # start of the region that may still move

    if prev_order is None:
        # The whole previous buffer was already sorted (identity). If appending the
        # new tail keeps it sorted across the junction, it stays identity.
        window = idx0[lo:]
        joins = (lo == 0) or (window[0] >= idx0[lo - 1])
        if joins and _is_sorted(window):
            return None, _state(None)
        keep_head = np.arange(lo)
        tail_old = np.arange(lo, prev_n)
    else:
        keep_head = prev_order[:max(0, prev_n - margin)]
        tail_old = prev_order[max(0, prev_n - margin):]

    new_raw = np.arange(prev_n, n)
    combine = np.concatenate((tail_old, new_raw))
    w = np.argsort(idx0[combine], kind='stable')
    order = np.concatenate((keep_head, combine[w]))
    return order, _state(order)


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
        self.result_channels = 2
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(RECV_TIMEOUT_S)
        self.cmd_sock.connect((ip, cmd_port))
        self.buffer = DeviceBuffer(max(self.channels, self.result_channels))
        self.id = int(ip.split('.')[3])
        self.header_struct = struct.Struct('<HH')
        self.silent_ping = False
        self.capture_active = False
        self.capture_counter = 0
        self.capture_limit = 0
        self.ptp_triggered = False
        self.received_last = 0
        self.last_data_time = 0.0
        # Time of the last packet of ANY type received from this device (data, result,
        # log or ACK). Used by the firewall keepalive to decide when the pinhole for the
        # data/log port needs re-opening; distinct from last_data_time (which must only
        # track real data for the stall watchdog, not logs/ACKs).
        self.last_recv_time = 0.0
        self.last_ack_time = 0.0
        self.input_packet_ring = deque(maxlen=PTP_TRIGGER_RING_PACKETS)
        self.first_data_order = None
        self.last_data_order = None
        self.packet_index = 0
        self.trigger_order = None
        self.trigger_sample_num = 0
        self.pretrigger_packets = 0
        self.last_fault_state = 0
        self.last_fault_latched = 0
        self.last_fault_valid = False
        # Bumped on every incoming packet that refreshes the live fault/analog values,
        # even when nothing is appended to the plot buffer (e.g. PTP mode waiting for a
        # trigger). Lets the GUI refresh the indicators/labels independently of
        # buffer.revision (which only advances while data is captured into the plot).
        self.live_revision = 0
        self.digital_channels = []  # descriptors from CMD_GET_DIGITAL_CHANNELS
        self.last_analog_values = [0.0] * self.channels
        self.last_analog_valid = False
        self.analog_units = ['-'] * self.channels
        self.analog_decimals = [3] * self.channels
        self.analog_widths = [8] * self.channels

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
            self.channels = int(info['channels_count'])
            self.result_channels = int(info.get('fault_state_count', self.channels))
            self.buffer = DeviceBuffer(max(self.channels, self.result_channels))
            self.info = info
            self._build_analog_display_meta()
            self._log_channel_calibration()
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

    def reset_fault_state(self, mask:int|None = None):
        payload = b'' if mask is None else struct.pack('<H', mask & 0xFFFF)
        return self._send_cmd(int(CMD.RESET_FAULT_STATE), payload)

    def get_digital_channels(self):
        """CMD_GET_DIGITAL_CHANNELS -> list of per-bit descriptors, or None.

        Each entry: {'label', 'flags', 'has_current', 'has_latched'}. The wire
        payload (after the 8-byte ACK header) is a digital_channels_packet_t:
        count(1) + reserved(3) + count x digital_channel_info_t(label[4], flags, reserved).
        """
        res = self._send_cmd_with_ack(int(CMD.GET_DIGITAL_CHANNELS))
        if res is None:
            self._logger.warning(f"Dev {self.ip} failed to get digital channels (no/invalid ACK).")
            return None
        _state, extra = res
        hdr = STRUCT.DIGITAL_CHANNELS_HEADER
        ch_struct = STRUCT.DIGITAL_CHANNEL
        if len(extra) < hdr.size:
            self._logger.warning(f"Dev {self.ip} digital channels reply too short ({len(extra)}).")
            return None
        count = extra[0]
        channels = []
        for i in range(count):
            off = hdr.size + i * ch_struct.size
            if off + ch_struct.size > len(extra):
                break
            label_raw, flags, _reserved = ch_struct.unpack_from(extra, off)
            label = label_raw.decode('ascii', errors='ignore').replace('\x00', '').strip()
            channels.append({
                'label': label or str(i),
                'flags': flags,
                'has_current': bool(flags & DIGITAL_CHANNEL_FLAG_CURRENT),
                'has_latched': bool(flags & DIGITAL_CHANNEL_FLAG_LATCHED),
            })
        self.digital_channels = channels
        return channels

    def _channel_calibration(self, channel_idx: int):
        info = getattr(self, 'info', None) or {}
        ch_infos = info.get('channels', [])
        if channel_idx < len(ch_infos):
            ch = ch_infos[channel_idx]
            gain = float(ch.get('gain', 1.0))
            offset = float(ch.get('offset', 0.0))
            # Unit is decoded to a str once in parse_id_packet.
            unit = str(ch.get('unit', '')).strip()
            return gain, offset, (unit or '-')
        return 1.0, 0.0, '-'

    def _build_analog_display_meta(self):
        self.analog_units = []
        self.analog_decimals = []
        self.analog_widths = []
        for ch_idx in range(self.channels):
            gain, offset, unit = self._channel_calibration(ch_idx)
            abs_gain = abs(gain)
            if not np.isfinite(abs_gain) or abs_gain <= 0:
                decimals = 3
            else:
                decimals = int(max(0, min(6, np.ceil(-np.log10(abs_gain)))))

            lo = -32768.0 * gain + offset
            hi = 32767.0 * gain + offset
            max_abs = max(abs(lo), abs(hi), abs(offset))
            if not np.isfinite(max_abs) or max_abs < 1.0:
                int_digits = 1
            else:
                int_digits = int(np.floor(np.log10(max_abs))) + 1
            sign_chars = 1 if (lo < 0 or hi < 0 or offset < 0) else 0
            width = sign_chars + int_digits + (1 + decimals if decimals > 0 else 0)

            self.analog_units.append(unit)
            self.analog_decimals.append(decimals)
            self.analog_widths.append(max(4, min(14, width)))

        if len(self.last_analog_values) != self.channels:
            self.last_analog_values = [0.0] * self.channels

    def _log_channel_calibration(self):
        parts = []
        for ch_idx in range(self.channels):
            gain, offset, unit = self._channel_calibration(ch_idx)
            decimals = self.analog_decimals[ch_idx] if ch_idx < len(self.analog_decimals) else 3
            width = self.analog_widths[ch_idx] if ch_idx < len(self.analog_widths) else 8
            parts.append(
                f'ch{ch_idx}: offset={offset:.6g}, gain={gain:.6g}, unit={unit}, fmt=width{width}/dec{decimals}'
            )
        if parts:
            self._logger.info(f'ID calibration {self.ip}: ' + '; '.join(parts))

    def _update_analog_values_from_raw_last(self, raw_last_values):
        vals = []
        for ch_idx in range(self.channels):
            gain, offset, _unit = self._channel_calibration(ch_idx)
            raw_v = float(raw_last_values[ch_idx]) if ch_idx < len(raw_last_values) else 0.0
            vals.append(raw_v * gain + offset)
        self.last_analog_values = vals
        self.last_analog_valid = True

    def _update_analog_values_from_data_packet(self, data: bytes):
        nsamp = self.channels * SAMPLES_PER_PACKET
        raw = np.frombuffer(data, dtype='<i2', count=nsamp, offset=12)
        if raw.size != nsamp:
            return
        raw = raw.reshape(self.channels, SAMPLES_PER_PACKET)
        self._update_analog_values_from_raw_last(raw[:, -1])

    def _update_fault_words_from_data_packet(self, data: bytes):
        off = 12 + 2 * self.channels * SAMPLES_PER_PACKET + self.channels
        off += self.channels % 2
        if off + 2 <= len(data):
            self.last_fault_state = struct.unpack('<H', data[off:off+2])[0]
            off += 2
        else:
            self.last_fault_state = 0
        if _uses_v5_packet_format(getattr(self, 'info', None)) and off + 2 <= len(data):
            self.last_fault_latched = struct.unpack('<H', data[off:off+2])[0]
        else:
            self.last_fault_latched = 0
        self.last_fault_valid = True

    def _update_live_values_from_data_packet(self, data: bytes):
        self._update_fault_words_from_data_packet(data)
        self._update_analog_values_from_data_packet(data)
        self.live_revision += 1

    def _update_live_values_from_result_packet(self, data: bytes):
        # CCU RESULT packets carry the combined fault-output bitmask in `value`
        # (offset 12); mirror it into the live fault state so the CCU's fault
        # indicators colour like the nodes' (the CCU never sends DATA packets, so
        # its live values would otherwise never update). No separate latched word
        # exists for the CCU's own outputs, so latched stays 0.
        val_off = 12
        if val_off + 2 > len(data):
            return
        self.last_fault_state = struct.unpack('<H', data[val_off:val_off+2])[0]
        self.last_fault_latched = 0
        self.last_fault_valid = True
        self.live_revision += 1
    
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
        # Any inbound packet refreshes the firewall pinhole for the data/log port.
        self.last_recv_time = time.monotonic()
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
                            data = _verify_crc(pkt)
                            if data not in (None, False):
                                self._update_live_values_from_data_packet(data)
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
                self._update_live_values_from_data_packet(data)
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
                            data = _verify_crc(pkt)
                            if data not in (None, False):
                                self._update_live_values_from_result_packet(data)
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

                for bit_idx in range(self.result_channels):
                    bit_vals = (result_code >> bit_idx) & 1
                    samples.append([bit_vals])

                # result_packet_t layout after value, derived from the shared
                # constants GATHERING_DEVICES and ACQUISITION_CHANNELS. FW v5+
                # inserts fault_latched[GATHERING_DEVICES] after fault_state.
                fs_off = val_off + 2
                fl_off = fs_off + GATHERING_DEVICES * 2
                if _uses_v5_packet_format(getattr(self, 'info', None)):
                    pe_off = fl_off + GATHERING_DEVICES * 2
                    fault_latched = struct.unpack(f'<{GATHERING_DEVICES}H', data[fl_off:pe_off])
                else:
                    pe_off = fl_off
                    fault_latched = (0,) * GATHERING_DEVICES
                cem_off = pe_off + GATHERING_DEVICES * ACQUISITION_CHANNELS
                fault_state = struct.unpack(f'<{GATHERING_DEVICES}H', data[fs_off:fl_off])
                parity_errors = tuple(data[pe_off:cem_off])
                crc_error_mask = data[cem_off] if cem_off < len(data) else 0
                # Map packet parity counters to displayed result channels.
                errs = list(parity_errors[:self.result_channels])
                if len(errs) < self.result_channels:
                    errs.extend([0] * (self.result_channels - len(errs)))

                self._update_live_values_from_result_packet(data)
                self.loop.call_soon_threadsafe(self.buffer.extend_result, t, samples, errs,
                                               ptp, fault_state, fault_latched, parity_errors, crc_error_mask)
                #self._logger.info(f"Dev {self.ip} packetNumber[{order}]: result {result_code}")
                return order
            case self.PKT_TYPE_ID:
                self._logger.info(f"Dev {self.ip} ID packet received on data socket.")
                self._parse_id(pkt)
                return

# Manager of multiple devices
class DeviceManager:
    MAX_DEVICES = 6
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
    
    def register_all(self, addr:str, port:int, exclude_ips=None):
        exclude = set(exclude_ips or ())
        for ip, dev in self.devices.items():
            if ip in exclude:
                continue
            dev.register_receiver(addr, port)

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

    def get_digital_channels_all(self):
        return {ip: dev.get_digital_channels() for ip, dev in self.devices.items()}

    def reset_counter(self):
        return self._send_cmd_broadcast(10)      

    def reset_fault_state(self, ip:str|None = None, mask:int|None = None):
        dev = self.devices.get(ip) if ip is not None else self.ccu_device()
        return dev.reset_fault_state(mask=mask) if dev is not None else None

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
        self.device_fault_indicator_layouts: List[QHBoxLayout] = []
        self.device_fault_indicators: List[List[QPushButton]] = []
        self.device_analog_value_layouts: List[QHBoxLayout] = []
        self.device_analog_value_labels: List[List[QLabel]] = []

        for i in range(DeviceManager.MAX_DEVICES):
            lb = QLabel(DEVICE_LABELS[i] if i < len(DEVICE_LABELS) else f'Device {i}')
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

            fault_box = QHBoxLayout()
            fault_box.setContentsMargins(0, 0, 0, 0)
            fault_box.setSpacing(4)
            fault_widget = QWidget()
            fault_widget.setLayout(fault_box)
            fault_widget.setToolTip('Digital fault outputs')
            cfg.addWidget(fault_widget, i, 6, alignment=Qt.AlignLeft | Qt.AlignVCenter)
            self.device_fault_indicator_layouts.append(fault_box)
            self.device_fault_indicators.append([])

            analog_box = QHBoxLayout()
            analog_box.setContentsMargins(0, 0, 0, 0)
            analog_box.setSpacing(10)
            analog_widget = QWidget()
            analog_widget.setLayout(analog_box)
            analog_widget.setToolTip('Live analog values')
            cfg.addWidget(analog_widget, i, 7, alignment=Qt.AlignLeft | Qt.AlignVCenter)
            self.device_analog_value_layouts.append(analog_box)
            self.device_analog_value_labels.append([])

        self.leader_buttons.buttonClicked[int].connect(self._leader_changed)

        cfg.addWidget(QLabel('Receiver addr:port'), 0, 8)
        self.receiver_edit = QLineEdit(f'0.0.0.0:{DEFAULT_DATA_PORT}')
        cfg.addWidget(self.receiver_edit, 0, 9)

        cfg.addWidget(QLabel('Measurement number'), 2, 8)
        self.measurement_number_edit = QLineEdit(f'0')
        cfg.addWidget(self.measurement_number_edit, 2, 9)

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
        # Fixed width so the buttons after it don't shift as the status text changes
        # length. Sized to a representative long status string.
        self.system_status_lbl.setFixedWidth(
            self.system_status_lbl.fontMetrics().horizontalAdvance('System: state unknown (no response)') + 8)
        btns.addWidget(self.system_status_lbl)

        pretrigger_lbl = QLabel('Pre-trigger packets:')
        pretrigger_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        btns.addWidget(pretrigger_lbl)
        self.pretrigger_spin = QSpinBox()
        self.pretrigger_spin.setRange(0, PTP_TRIGGER_RING_PACKETS)
        self.pretrigger_spin.setValue(0)
        btns.addWidget(self.pretrigger_spin)

        posttrigger_lbl = QLabel('Post-trigger packets:')
        posttrigger_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        btns.addWidget(posttrigger_lbl)
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
                          ('Reset Latched Faults'           , self._reset_fault_state               ),
                          #('Clean Graf'                     , self.clear_plot                       ),
                          #('Penetrate Firewall'             , self._penetrate_firewall              ),
                          #('Save Data'                      , self.save_data                        ),
                          ('Reset devices'                   , self._reset_devices                   ),
                          ):
            b=QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)

        # Plot downsampling / rendering controls. setDownsampling(ds, auto, mode)
        # reduces how many points pyqtgraph actually draws each frame, the dominant
        # cost for multi-million-sample traces. 'mode' picks the algorithm; the
        # factor spinbox is the fixed decimation factor, or 0 to let pyqtgraph pick
        # it automatically from the visible pixel width (auto=True).
        downsample_lbl = QLabel('Downsample:')
        downsample_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        btns.addWidget(downsample_lbl)
        self.downsample_mode_combo = QComboBox()
        self.downsample_mode_combo.addItems(['Off', 'Subsample', 'Mean', 'Peak'])
        self.downsample_mode_combo.setCurrentText('Peak')
        self.downsample_mode_combo.setToolTip(
            'Off: draw every point. Subsample: every Nth point (fast, can miss spikes). '
            'Mean: average each group. Peak: min/max envelope (preserves spikes).')
        btns.addWidget(self.downsample_mode_combo)
        self.downsample_factor_spin = QSpinBox()
        self.downsample_factor_spin.setRange(0, 100000)
        self.downsample_factor_spin.setValue(0)
        self.downsample_factor_spin.setToolTip('Decimation factor; 0 = auto (chosen from the visible pixel width).')
        btns.addWidget(self.downsample_factor_spin)
        self.clip_to_view_chk = QCheckBox('Clip to view')
        self.clip_to_view_chk.setChecked(True)
        self.clip_to_view_chk.setToolTip('Only draw the part of each curve inside the visible x-range (huge win when zoomed in).')
        btns.addWidget(self.clip_to_view_chk)

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
        # When OpenGL curve painting is enabled (pg.setConfigOptions(useOpenGL=True)),
        # the curves use paintGL, which only draws if the view's viewport is an OpenGL
        # widget. Without this the GL calls go nowhere and the curves are invisible.
        if USE_OPENGL:
            self.plot_widget.useOpenGL(True)
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
        # Last per-device live_revision reflected in the indicators/labels. These
        # advance on every incoming packet (even when the plot buffer is idle), so the
        # indicators refresh whenever data flows, independently of the plot redraw.
        self._live_revisions = None
        # Deferred grey-out after a stop/stall transition: wait two GUI refreshes and
        # grey the indicators only if NO new packet arrived in between, so a genuinely
        # idle system greys out while a stopped one still fed sporadic (e.g.
        # test/spoofed) packets keeps colouring. _grey_pending = remaining refresh
        # countdown; _grey_baseline_rev = total received-packet counter snapshot used
        # to detect new data; _system_status_ok = last status was RUNNING/OK, so we
        # arm only on an OK -> non-OK transition (one arm per start+stop cycle).
        self._grey_pending = 0
        self._grey_baseline_rev = None
        self._system_status_ok = False
        # Per-device incremental sort state for plot_sort_order(): each frame only the
        # newly appended tail (+ overlap) is re-sorted instead of the whole buffer.
        self._sort_state: Dict[str, dict] = {}

        # Detection plot
        self.plot_widget.nextRow()  # move to next row in the graphics layout
        self.ax_result = self.plot_widget.addPlot(title='Detection result')
        self.ax_result.showGrid(x=True, y=True, alpha=0.5)
        self.ax_result.setLabel('bottom', 'Time', units='s')
        self.ax_result.setLabel('left', 'Errors')
        self.ax_result.addLegend()
        # Link x-axis so both plots share the same time base and zoom/pan together
        self.ax_result.setXLink(self.ax)
        # Curves for detection-result bits keyed by (source, bit index), e.g.
        # ('ccu', 0) or ('node2', 1).
        self.ax_result_curves: Dict[Tuple[str, int], pg.PlotDataItem] = {}
        self.ax_result.setYRange(0, 1)

        # Now that both plots exist, wire the downsampling controls and apply once.
        self.downsample_mode_combo.currentIndexChanged.connect(self._apply_downsampling)
        self.downsample_factor_spin.valueChanged.connect(self._apply_downsampling)
        self.clip_to_view_chk.toggled.connect(self._apply_downsampling)
        self._apply_downsampling()

        self.error_lbl = QLabel()
        self.error_lbl.setStyleSheet('font-family: monospace')
        root.addWidget(self.error_lbl)

        # Collapsible log pane: a toggle button lets the user hide the log so it
        # doesn't take up graph area during normal operation.
        self.log_toggle_btn = QPushButton()
        self.log_toggle_btn.setCheckable(True)
        self.log_toggle_btn.setFlat(True)
        self.log_toggle_btn.setStyleSheet('text-align:left; padding:2px')
        self.log_toggle_btn.toggled.connect(self._toggle_log)
        root.addWidget(self.log_toggle_btn)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet('font-family: monospace; background:#f0f0f0')

        self.log_scroll = QScrollArea()
        self.log_scroll.setWidgetResizable(True)
        self.log_scroll.setWidget(self.log_output)
        root.addWidget(self.log_scroll)

        self.log_signal.connect(self.log_output.append)

        # Start collapsed so the graph gets the full area during normal operation.
        self.log_toggle_btn.setChecked(False)
        self._toggle_log(False)

        # Keep the firewall pinhole for the data/log port open. Device logs share the
        # data socket and can be rarer than the firewall's idle timeout, so re-ping any
        # device that has been silent for FIREWALL_KEEPALIVE_TIMEOUT_S. Checked once a
        # second so the ping goes out promptly once the idle threshold is crossed.
        self.firewall_keepalive_timer = QTimer(self)
        self.firewall_keepalive_timer.setInterval(1000)
        self.firewall_keepalive_timer.timeout.connect(self._firewall_keepalive)
        self.firewall_keepalive_timer.start()

        self.timer = QTimer(self)
        self.timer.setInterval(GUI_REFRESH_INTERVAL_MS)
        self.timer.timeout.connect(self._update_plot)
        self.timer.start()

        self.data_ready.connect(self._check_order)
        self.trigger_received.connect(self._flash_device_trigger)
        self.manager.set_trigger_signal(self.trigger_received)

    def closeEvent(self, event):
        self.manager.shutdown()
        super().closeEvent(event)

    def _toggle_log(self, checked: bool):
        """Show or hide the log pane. When hidden the graph reclaims the space."""
        self.log_scroll.setVisible(checked)
        self.log_toggle_btn.setText(('▼' if checked else '▶') + ' Log')

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

    def _configured_ip_for_row(self, row:int) -> str | None:
        txt = self.device_edits[row].text().strip()
        if not txt:
            return None
        return txt.split(':')[0]

    def _device_for_row(self, row:int) -> Device | None:
        ip = self._configured_ip_for_row(row)
        return self.manager.devices.get(ip) if ip else None

    def _fault_indicator_style(self, fault:int|None, latch:int|None, has_label:bool=False) -> str:
        if fault is None or latch is None:
            color = FAULT_INDICATOR_UNKNOWN
            text_color = 'black'
        else:
            color = FAULT_INDICATOR_COLORS[(fault, latch)]
            text_color = 'white' if (fault, latch) in ((0, 0), (1, 1)) else 'black'
        radius = FAULT_INDICATOR_SIZE // 2
        if has_label:
            size = (f'min-width: {FAULT_INDICATOR_SIZE}px; padding: 0px 6px; '
                    f'min-height: {FAULT_INDICATOR_SIZE}px; max-height: {FAULT_INDICATOR_SIZE}px;')
        else:
            size = (f'min-width: {FAULT_INDICATOR_SIZE}px; max-width: {FAULT_INDICATOR_SIZE}px; '
                    f'min-height: {FAULT_INDICATOR_SIZE}px; max-height: {FAULT_INDICATOR_SIZE}px; padding: 0px;')
        return (
            'QPushButton {'
            f'background-color: {color}; color: {text_color}; border: 1px solid #555; '
            f'border-radius: {radius}px; {size}'
            '}'
        )

    def _rebuild_fault_indicator_row(self, row:int, labels:List[str]):
        layout = self.device_fault_indicator_layouts[row]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        buttons: List[QPushButton] = []
        for bit_idx, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._fault_indicator_style(None, None, has_label=len(label) > 1))
            btn.clicked.connect(lambda _checked=False, row=row, bit=bit_idx: self._reset_fault_bit(row, bit))
            layout.addWidget(btn)
            buttons.append(btn)
        layout.addStretch(1)
        self.device_fault_indicators[row] = buttons

    def _digital_bit_label(self, dev, bit_idx:int) -> str | None:
        """Digital-channel mnemonic for a fault-output bit, or None when only the
        plain numeric index is available."""
        descriptors = getattr(dev, 'digital_channels', None) if dev is not None else None
        if descriptors and bit_idx < len(descriptors):
            label = descriptors[bit_idx].get('label')
            if label and label != str(bit_idx):
                return label
        return None

    def _fault_indicator_labels(self, dev) -> List[str]:
        """Per-bit button labels: digital-channel mnemonics when available,
        otherwise the plain bit index."""
        descriptors = getattr(dev, 'digital_channels', None) if dev is not None else None
        if descriptors:
            labels = [d.get('label') or str(i) for i, d in enumerate(descriptors)]
        else:
            info = getattr(dev, 'info', None) or {}
            bit_count = int(info.get('fault_state_count', getattr(dev, 'result_channels', 0)))
            labels = [str(i) for i in range(bit_count)]
        return labels[:16]

    def _refresh_fault_indicators(self):
        for row in range(DeviceManager.MAX_DEVICES):
            dev = self._device_for_row(row)
            labels = self._fault_indicator_labels(dev)
            current = [b.text() for b in self.device_fault_indicators[row]]
            if current != labels:
                self._rebuild_fault_indicator_row(row, labels)
            if not labels:
                continue

            descriptors = getattr(dev, 'digital_channels', None) if dev is not None else None
            valid = bool(dev and dev.last_fault_valid)
            fault_word = int(getattr(dev, 'last_fault_state', 0)) if valid else 0
            latch_word = int(getattr(dev, 'last_fault_latched', 0)) if valid else 0
            for bit_idx, btn in enumerate(self.device_fault_indicators[row]):
                has_label = len(btn.text()) > 1
                if descriptors and bit_idx < len(descriptors):
                    caps = ('current' if descriptors[bit_idx]['has_current'] else '') \
                        + ('+latched' if descriptors[bit_idx]['has_latched'] else '')
                    caps = caps.strip('+') or 'none'
                    name = f"'{descriptors[bit_idx]['label']}' (bit {bit_idx}, {caps})"
                else:
                    name = f'Bit {bit_idx}'
                if valid:
                    fault = (fault_word >> bit_idx) & 1
                    latch = (latch_word >> bit_idx) & 1
                    btn.setStyleSheet(self._fault_indicator_style(fault, latch, has_label=has_label))
                    btn.setToolTip(f'{name}: fault={fault}, latch={latch}. Click to reset this latch bit.')
                else:
                    btn.setStyleSheet(self._fault_indicator_style(None, None, has_label=has_label))
                    btn.setToolTip(f'{name}: no live fault data yet. Click to reset this latch bit.')

    def _rebuild_analog_value_row(self, row:int, channel_count:int):
        layout = self.device_analog_value_layouts[row]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        labels: List[QLabel] = []
        for _ch in range(channel_count):
            lbl = QLabel()
            lbl.setStyleSheet(ANALOG_VALUE_FONT_STYLE)
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            layout.addWidget(lbl)
            labels.append(lbl)
        layout.addStretch(1)
        self.device_analog_value_labels[row] = labels

    def _format_analog_value(self, dev: Device, channel_idx: int) -> str:
        unit = dev.analog_units[channel_idx] if channel_idx < len(dev.analog_units) else '-'
        width = dev.analog_widths[channel_idx] if channel_idx < len(dev.analog_widths) else 8
        decimals = dev.analog_decimals[channel_idx] if channel_idx < len(dev.analog_decimals) else 3
        if dev.last_analog_valid and channel_idx < len(dev.last_analog_values):
            val = float(dev.last_analog_values[channel_idx])
            if np.isfinite(val):
                val_s = f'{val:>{width}.{decimals}f}'
            else:
                val_s = f"{'nan':>{width}}"
        else:
            val_s = f"{'--':>{width}}"
        return f'{val_s} {unit}'

    def _refresh_analog_values(self):
        for row in range(DeviceManager.MAX_DEVICES):
            dev = self._device_for_row(row)
            channel_count = int(getattr(dev, 'channels', 0)) if dev is not None else 0
            if len(self.device_analog_value_labels[row]) != channel_count:
                self._rebuild_analog_value_row(row, channel_count)
            if channel_count == 0:
                continue
            valid = bool(dev and dev.last_analog_valid)
            for ch_idx, lbl in enumerate(self.device_analog_value_labels[row]):
                lbl.setText(self._format_analog_value(dev, ch_idx))
                if valid:
                    lbl.setStyleSheet(ANALOG_VALUE_FONT_STYLE)
                    lbl.setToolTip(f'Channel {ch_idx} live value (calibrated)')
                else:
                    lbl.setStyleSheet(ANALOG_VALUE_NO_DATA_STYLE)
                    lbl.setToolTip(f'Channel {ch_idx}: no live data')

    def _arm_grey_check(self):
        """Arm the deferred grey-out: over the next two GUI refreshes, grey the
        indicators only if no new packet arrives in between (see _update_plot). Lets a
        genuinely idle system grey out while a stopped one still receiving sporadic
        (e.g. test/spoofed) packets keeps colouring. A lone packet arriving long after
        the stop colours the indicators and they stay coloured until the next
        start + stop re-arms the check."""
        self._grey_pending = 2
        self._grey_baseline_rev = None

    def _reset_fault_bit(self, row:int, bit_idx:int):
        dev = self._device_for_row(row)
        if dev is None:
            self._logger.warning(f'No device mapped to row {row} for fault reset.')
            return
        mask = 1 << bit_idx
        if self.manager.reset_fault_state(ip=dev.ip, mask=mask):
            self._logger.info(f'Reset latched fault bit {bit_idx} on {dev.ip} (mask=0x{mask:04X})')
        else:
            self._logger.error(f'Reset latched fault bit {bit_idx} on {dev.ip} was not acknowledged (mask=0x{mask:04X})')

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
        self._refresh_fault_indicators()
        self._refresh_analog_values()
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
            self._logger.info(f'ID {ip}: ' + (
                f"FW {info['fw_id']} v{info['fw_ver_major']}.{info['fw_ver_minor']}; "
                f"channels={info['channels_count']}; faults={info.get('fault_state_count', 0)}; "
                f"latched={info.get('fault_latched_count', 0)}" if info else 'FAIL'))
        self._refresh_fault_indicators()
        self._refresh_analog_values()

    def _get_digital_channels(self):
        for ip, chans in self.manager.get_digital_channels_all().items():
            if chans is None:
                self._logger.info(f'Digital channels {ip}: FAIL')
                continue
            desc = ', '.join(
                f"{i}:'{c['label']}'(" + ('C' if c['has_current'] else '') + ('L' if c['has_latched'] else '') + ')'
                for i, c in enumerate(chans))
            self._logger.info(f'Digital channels {ip}: count={len(chans)}; {desc}')
        self._refresh_fault_indicators()

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
            # The CCU is not registered on the ISOMON device (the CCU knows nothing about it).
            isomon_ip = self.device_edits[ISOMON_DEVICE_INDEX].text().strip().split(':')[0]
            self.manager.register_all(addr, port, exclude_ips=[isomon_ip])
            self._logger.info(f'Registered CCU {addr}:{port} on all nodes except ISOMON ({isomon_ip})')
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

    def _apply_downsampling(self, *_):
        """Apply the GUI downsampling / clip-to-view settings to both plots.

        mode 'Off'        -> draw every point (no decimation).
        otherwise         -> setDownsampling(mode=<algo>) with either a fixed factor
                             (spinbox >= 1) or auto-decimation (spinbox == 0, auto=True).
        """
        mode = self.downsample_mode_combo.currentText().lower()
        factor = self.downsample_factor_spin.value()
        clip = self.clip_to_view_chk.isChecked()
        # The factor spinbox only makes sense for a fixed factor; grey it out for 'auto'
        # is handled by the user (0 == auto), so just translate the settings here.
        for plot in (self.ax, self.ax_result):
            if mode == 'off':
                plot.setDownsampling(ds=1, auto=False)
            elif factor <= 0:
                plot.setDownsampling(auto=True, mode=mode)
            else:
                plot.setDownsampling(ds=factor, auto=False, mode=mode)
            plot.setClipToView(clip)

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
        # Drive the live indicators from the system status. A RUNNING/OK status lets
        # incoming packets colour them; any other status (stopped/idle/failed/stalled)
        # ARMS a deferred grey-out that only greys if data actually stopped (no new
        # packet across two refreshes, see _update_plot). This keeps a stopped-but-
        # still-fed system (e.g. test/spoofed faults) coloured, while a genuinely idle
        # one greys out. Armed only on an OK -> non-OK transition (one per stop).
        if color:
            ok = (color == SYSTEM_STATUS_COLOR_OK)
            if ok:
                self._grey_pending = 0          # cancel a pending grey; packets colour
            elif self._system_status_ok:        # OK -> non-OK transition: arm the check
                self._arm_grey_check()
            self._system_status_ok = ok

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

    def _reset_fault_state(self):
        if self.manager.reset_fault_state():
            self._logger.info('Reset latched faults on CCU')
        else:
            self._logger.error('Reset latched faults command not acknowledged by CCU')

    def _force_trigger(self):
        #self.manager.broadcast('force_trigger')
        #self._logger.info('Force trigger on all devices')
        #leader_id = self.leader_buttons.checkedId()
        #leader_ip = self.device_edits[leader_id].text().strip().split(':')[0]
        self.manager.force_trigger()

    def _penetrate_firewall(self):
        self._logger.info('Trying to penetrate firewall')
        self.manager.penetrate_firewall(False)

    def _firewall_keepalive(self):
        """Keep the firewall pinhole open for the device log stream (PACKET_LOG).

        The firmware delivers logs to SCADA's data socket but sends them FROM the device
        COMMAND port, so the firewall (which keys on both endpoints' ports) does not
        cover them with the data stream's pinhole. Re-send a ping out of the data socket
        to the device command port whenever a device has been silent (no data/log/ACK)
        for FIREWALL_KEEPALIVE_TIMEOUT_S; the ping's ACK (or any real packet) refreshes
        last_recv_time, so an actively streaming device is skipped. Gated by
        FIREWALL_PENETRATION: 'off' disables it, 'verbose' makes the ping non-silent
        (visible in the device log), otherwise the ping is silent."""
        if FIREWALL_PENETRATION == 'off':
            return
        if not self.manager.devices or self.manager.data_socket is None:
            return
        now = time.monotonic()
        silent = FIREWALL_PENETRATION != 'verbose'
        for dev in self.manager.devices.values():
            if dev.last_recv_time and (now - dev.last_recv_time) < FIREWALL_KEEPALIVE_TIMEOUT_S:
                continue
            dev.ping(self.manager.data_socket, silent=silent, port=dev.cmd_port)
    
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
                dev.buffer.result_fault_latched.clear()
                dev.buffer.result_parity_errors.clear()
                dev.buffer.result_crc_error_mask.clear()
        self.ax.clear()
        self.curves.clear()

        #if hasattr(self, 'ax_result'):
        self.ax_result.clear()
        self.ax_result_curves.clear()

        self._sort_state.clear()
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
                result_fault_state = list(dev.buffer.result_fault_state)
                result_fault_latched = list(dev.buffer.result_fault_latched)
                result_parity_errors = list(dev.buffer.result_parity_errors)
                result_crc_error_mask = list(dev.buffer.result_crc_error_mask)

            # CCU devices carry per-result-packet metadata; nodes leave these empty.
            has_result_meta = bool(result_crc_error_mask)
            csv_channels = dev.result_channels if has_result_meta else dev.channels
            signals_a = [np.asarray(dev.buffer.signal[c + 1]) for c in range(csv_channels)]

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
                lengths += [len(result_fault_state), len(result_fault_latched), len(result_parity_errors), len(result_crc_error_mask)]
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
                if has_result_meta:
                    ch_headers = [f'ch{c}' for c in range(csv_channels)]
                else:
                    # Analog channels: append the calibrated unit in the same
                    # '[unit]' format as the plot legend (skip the '-' placeholder).
                    ch_headers = []
                    for c in range(csv_channels):
                        unit = dev.analog_units[c] if c < len(dev.analog_units) else ''
                        ch_headers.append(f'ch{c} [{unit}]' if unit and unit != '-' else f'ch{c}')
                header = ['time', 'ptp_ns'] + ch_headers
                if has_result_meta:
                    n_fault = GATHERING_DEVICES
                    n_parity = GATHERING_DEVICES * ACQUISITION_CHANNELS
                    header += [f'fault_state{d}' for d in range(n_fault)]
                    header += [f'fault_latched{d}' for d in range(n_fault)]
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
                        row = [sig_lists[ch][i] for ch in range(csv_channels)]
                        row += list(result_fault_state[i])
                        row += list(result_fault_latched[i])
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
                        tmpl = '%.6f,' + ','.join(['%d'] * (csv_channels + 1))
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
        # Export celé GUI části s oběma grafy pod sebou.
        # On some systems grab() can leave the viewport visually stale until
        # the next input event, so flush paints before grabbing and force a
        # repaint right after saving.
        self.plot_widget.viewport().repaint()
        QApplication.processEvents()
        pixmap = self.plot_widget.grab()
        if not pixmap.save(png_name, "PNG"):
            raise RuntimeError(f"Nepodařilo se uložit obrázek {png_name}.")

        self.plot_widget.viewport().update()
        self.plot_widget.update()
        self._update_plot(force=True)
        QApplication.processEvents()

        if strict:
            if not os.path.exists(png_name):
                raise RuntimeError(f"Nepodařilo se vytvořit obrázek {png_name}.")
            if os.path.getsize(png_name) == 0:
                raise RuntimeError(f"Obrázek {png_name} je prázdný.")
        
        files.append(png_name)

        self._logger.info('Saved data: ' + ', '.join(files))
        return files
        
    def _update_plot(self, force=False):
        # Deferred grey-out after a stop/stall transition (armed by _arm_grey_check):
        # wait two GUI refreshes and grey the indicators only if no new packet arrived
        # in between, so a stopped system still fed sporadic (e.g. test/spoofed)
        # packets keeps colouring while a genuinely idle one greys out.
        if self._grey_pending:
            total_rev = sum(dev.live_revision for dev in self.manager.devices.values())
            if self._grey_baseline_rev is None:
                # First refresh after arming: snapshot the received-packet counter
                # (trailing packets from just before the stop settle into it here).
                self._grey_baseline_rev = total_rev
                self._grey_pending -= 1
            else:
                # Second refresh: if no packet arrived since the snapshot, grey out.
                if total_rev == self._grey_baseline_rev:
                    for dev in self.manager.devices.values():
                        dev.last_fault_valid = False
                        dev.last_analog_valid = False
                    self._live_revisions = None  # force the refresh below to redraw
                self._grey_pending = 0
        # Indicators/labels reflect the latest LIVE fault/analog values, which update
        # on every incoming packet even when nothing is being appended to the plot
        # buffer (e.g. PTP mode waiting for a trigger). Refresh them whenever a new
        # packet bumped live_revision, independently of the plot's buffer.revision skip.
        live_revisions = {ip: dev.live_revision for ip, dev in self.manager.devices.items()}
        if force or self._live_revisions != live_revisions:
            self._live_revisions = live_revisions
            self._refresh_fault_indicators()
            self._refresh_analog_values()
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
                    idx0 = np.asarray(buf.signal[0])
                    # Sort the per-sample buffer by time so a late/replayed packet
                    # around the trigger cannot draw a zig-zag at the pre/post
                    # boundary. The column is append-only and only locally reordered,
                    # so this is done incrementally (new tail + overlap) and returns
                    # None whenever the data is already ordered (the common case).
                    order_perm, self._sort_state[ip] = plot_sort_order(
                        idx0, self._sort_state.get(ip))
                    idx = idx0.astype(float)
                    tsi = dev.trigger_sample_index()
                    if tsi is not None:
                        # Shift so the trigger sample sits at t = 0 (per-sample, on
                        # the unified buffer rather than per packet).
                        idx -= tsi
                    if order_perm is not None:
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
                    x = idx[trim:trim_end] * SAMPLING_PERIOD
                    avgs = [0] * dev.channels
                    for ch in range(dev.channels):
                        key = (ip, ch)

                        #y = np.array(buf.signal[ch + 1])[-len(x):]
                        raw = np.array(buf.signal[ch + 1], dtype=float)
                        if order_perm is not None:
                            raw = raw[order_perm]
                        raw = raw[trim:trim_end]
                        # Kalibrace z ID paketu
                        gain = 1.0
                        offset = 0.0
                        unit = ""
                        if hasattr(dev, "info"):
                            try:
                                gain = dev.info["channels"][ch]["gain"]
                                offset = dev.info["channels"][ch]["offset"]
                                # Unit is decoded to a str once in parse_id_packet.
                                unit = str(dev.info["channels"][ch]["unit"])
                            except Exception:
                                pass
                        if key not in self.curves:
                            pen = pg.mkPen(Plotter.Colors[len(self.curves)], width=2)
                            curve_name = f'{ip}[{ch}]'
                            if unit:
                                curve_name += f' [{unit}]'
                            self.curves[key] = self.ax.plot(pen=pen, name=curve_name)
                        # Přepočet pouze pro zobrazení
                        if gain == 1.0 and offset == 0.0:
                            y = raw
                        else:
                            y = raw * gain
                            y += offset

                        avgs[ch] = np.mean(y[-min(len(y), SAMPLES_PER_PACKET * DEFAULT_AVG_LEN_MS):])
                        self.curves[key].setData(x[-len(y):], y)
                
                    # Error calculation: only the most recent packet's worth of
                    # per-sample parity counts is shown, so read just that tail
                    # instead of materialising the whole (multi-million) ring.
                    errs = ','.join(str(int(buf.error[c].tail(SAMPLES_PER_PACKET).sum(dtype=np.int64)))
                                    for c in range(dev.channels))

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
                    offset_step = 0.05  # vertical spacing between bit lines

                    # Build all displayed digital series: CCU output bits from
                    # result_code (stored in signal[]) + per-node fault_state bits
                    # carried in RESULT metadata.
                    series: List[Tuple[Tuple[str, int], str, np.ndarray]] = []

                    # CCU digital outputs (legacy plotting path).
                    for bit_idx in range(dev.result_channels):
                        y_pkt = np.array(buf.signal[bit_idx + 1], dtype=float)
                        lbl = self._digital_bit_label(dev, bit_idx)
                        name = f'ccu_bit{bit_idx}' + (f' [{lbl}]' if lbl else '')
                        series.append((("ccu", bit_idx), name, y_pkt))

                    # Node digital outputs from fault_state[node_idx] bitfields.
                    node_devices = [(i, ip_n, dev_n) for i, (ip_n, dev_n) in enumerate(self.manager.devices.items())
                                    if i != CCU_DEVICE_INDEX]
                    fault_state_rows = list(buf.result_fault_state)
                    if fault_state_rows:
                        fault_state_arr = np.asarray(fault_state_rows, dtype=np.uint16)
                        if fault_state_arr.ndim == 1:
                            fault_state_arr = fault_state_arr.reshape(-1, 1)
                        node_slots = min(fault_state_arr.shape[1], len(node_devices))
                        for node_idx in range(node_slots):
                            node_ip = node_devices[node_idx][1]
                            node_dev = node_devices[node_idx][2]
                            node_bits = int(getattr(node_dev, 'result_channels', 0))
                            node_word = fault_state_arr[:, node_idx]
                            for bit_idx in range(node_bits):
                                y_pkt = ((node_word >> bit_idx) & 1).astype(float)
                                lbl = self._digital_bit_label(node_dev, bit_idx)
                                name = f'{node_ip}_bit{bit_idx}' + (f' [{lbl}]' if lbl else '')
                                series.append(((f"node{node_idx}", bit_idx), name, y_pkt))

                    center = (len(series) - 1) / 2.0 if series else 0.0
                    for series_idx, (curve_key, curve_name, y_pkt) in enumerate(series):
                        if curve_key not in self.ax_result_curves:
                            color = Plotter.Colors[len(self.ax_result_curves) % len(Plotter.Colors)]
                            self.ax_result_curves[curve_key] = self.ax_result.plot(pen=color, name=curve_name)

                        y = np.repeat(y_pkt, SAMPLES_PER_PACKET)  # Y interpolation - steps
                        offset = (series_idx - center) * offset_step
                        y_bits = (y + offset)[trim:trim_end]
                        self.ax_result_curves[curve_key].setData(x[-len(y_bits):], y_bits)
                    
                    # Error calculation
                    errs = ','.join(str(int(buf.error[c].tail(1).sum(dtype=np.int64))) for c in range(dev.result_channels))

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
        init_fns = (self._ping_all, self._register_logger_all, self._get_ids, self._get_digital_channels, self._get_clock_config, self._get_trigger_config, self._register_all, self._register_ccu, self._reset_counter)
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
        global FIREWALL_KEEPALIVE_TIMEOUT_S
        global DATA_SOCKET_RCVBUF_BYTES
        global TRIGGER_CAPTURE_MARGIN_PACKETS
        global BUFFER_LENGTH_S, BUFFER_SIZE, MAX_CAPTURE_PACKETS
        global USE_OPENGL
        global GUI_REFRESH_INTERVAL_MS

        DEFAULT_FIRST_IP = getattr(ds, 'DEFAULT_FIRST_IP', "192.168.137.100")
        DEFAULT_LEADER = getattr(ds, 'DEFAULT_LEADER', 1)
        DEVICES_COUNT = getattr(ds, 'DEVICES_COUNT', 5)
        # ISOMON is disabled by default: until it actually streams data, enabling it
        # would break the running-system status (a checked device with no data).
        ISOMON_ENABLED = bool(getattr(ds, 'ISOMON_ENABLED', False))
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
        GUI_REFRESH_INTERVAL_MS = int(getattr(ds, 'GUI_REFRESH_INTERVAL_MS', GUI_REFRESH_INTERVAL_MS))
        FIREWALL_PENETRATION = str(getattr(ds, 'FIREWALL_PENETRATION', FIREWALL_PENETRATION)).lower()
        if FIREWALL_PENETRATION not in FIREWALL_PENETRATION_MODES:
            logging_.logger.warning(f"Invalid FIREWALL_PENETRATION={FIREWALL_PENETRATION!r}; falling back to 'on'. "
                                    f"Valid options: {FIREWALL_PENETRATION_MODES}.")
            FIREWALL_PENETRATION = 'on'
        FIREWALL_KEEPALIVE_TIMEOUT_S = float(getattr(ds, 'FIREWALL_KEEPALIVE_TIMEOUT_S', FIREWALL_KEEPALIVE_TIMEOUT_S))

        USE_OPENGL = bool(getattr(ds, 'USE_OPENGL', USE_OPENGL))
        if USE_OPENGL:
            # pyqtgraph's GL curve painting (paintGL) needs PyOpenGL; without it the
            # curves silently fail to draw. Fall back to the CPU painter so the plot
            # stays visible instead of going blank.
            try:
                import OpenGL  # noqa: F401  (PyOpenGL)
            except ImportError:
                logging_.logger.warning("USE_OPENGL=True but PyOpenGL is not installed "
                                        "(pip install PyOpenGL); falling back to the CPU painter.")
                USE_OPENGL = False
        pg.setConfigOptions(useOpenGL=USE_OPENGL, enableExperimental=USE_OPENGL)

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
        logging_.logger.info(f"Application started with settings: DEFAULT_FIRST_IP={DEFAULT_FIRST_IP}, DEFAULT_LEADER={DEFAULT_LEADER}, DEVICES_COUNT={DEVICES_COUNT}, DEFAULT_AVG_LEN_MS={DEFAULT_AVG_LEN_MS}, DEFAULT_PRETRIGGER_PACKETS={DEFAULT_PRETRIGGER_PACKETS}, DEFAULT_POSTTRIGGER_PACKETS={DEFAULT_POSTTRIGGER_PACKETS}, PTP_TRIGGER_RING_PACKETS={PTP_TRIGGER_RING_PACKETS}, DEFAULT_PTP_MODE_ENABLED={ptp_mode.enabled}, SOCKET_BACKEND={SOCKET_BACKEND}, DATA_SOCKET_RCVBUF_BYTES={DATA_SOCKET_RCVBUF_BYTES}, TRIGGER_CAPTURE_MARGIN_PACKETS={TRIGGER_CAPTURE_MARGIN_PACKETS}, BUFFER_LENGTH_S={BUFFER_LENGTH_S}, FIREWALL_PENETRATION={FIREWALL_PENETRATION}, USE_OPENGL={USE_OPENGL}, GUI_REFRESH_INTERVAL_MS={GUI_REFRESH_INTERVAL_MS}, ISOMON_ENABLED={ISOMON_ENABLED}")
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
                    # ISOMON is enabled only when ISOMON_ENABLED; otherwise it stays off so
                    # a device with no data doesn't break the running-system status.
                    checkbox.setChecked(i < DEVICES_COUNT or (i == ISOMON_DEVICE_INDEX and ISOMON_ENABLED))
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

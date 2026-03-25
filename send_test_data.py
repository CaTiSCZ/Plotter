from email import header
import socket
import threading
import time
import struct
import numpy as np
import argparse
from enum import IntEnum, unique

from assign_id import TARGET_PORT

try:
    import default_settings as ds
    DEFAULT_IP = ds.DEFAULT_FIRST_IP
except ImportError:
    DEFAULT_IP = "192.168.137.100"

DEFAULT_CMD_PORT   = 10578

SAMPLES_PER_PACKET = 200

class IntEnumName(IntEnum):
    def __str__(self):
        return self.name

@unique
class PACKET(IntEnumName):
    ACK_packet              =  0
    ID_packet               =  1
    DATA_packet             =  2
    TRIGGER_packet          =  3
    LOG_packet              =  4

@unique
class CMD(IntEnumName):
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
    SET_CALIBRATION         = 16
    ENABLE_TEST_MODE        = 17
    DISABLE_TEST_MODE       = 18

class STRUCT:
    CMD = struct.Struct("<I")
    ACK = struct.Struct("<HHI")
    ID = struct.Struct("<HHHBBI3I HBB I HBB 8s 30s H")
    CHANNEL = struct.Struct("<4s ff")
    CRC = struct.Struct("<H")
    PORT = struct.Struct("<H")
    DATA_HEADER = struct.Struct("<HH")
    FAULT_STATE = struct.Struct("<H")

def load_csv(file_path: str) -> tuple[np.ndarray, list[str]]:
    """Load CSV file with automatic delimiter and header detection.
    
    Returns:
        tuple: (data array, column names list)
    """
    # Read first two lines to detect delimiter and header
    with open(file_path, 'r') as f:
        first_line = f.readline().strip()
        second_line = f.readline().strip() if f.readable() else ""
    
    # Detect delimiter: semicolon has priority (used with decimal comma)
    # If no semicolons present, use comma (with decimal point)
    delimiter = ';' if ';' in first_line else ','
    
    # Split first line and clean whitespace
    first_values = [v.strip() for v in first_line.split(delimiter)]
    
    # Detect if first line is header (try to parse as float)
    has_header = False
    try:
        # Try to parse first value as float
        test_val = first_values[0].replace(',', '.')  # handle decimal comma
        float(test_val)
    except (ValueError, IndexError):
        has_header = True
    
    # Prepare converter for decimal comma if using semicolon
    if delimiter == ';':
        converter = lambda s: float(s.strip().replace(',', '.'))
    else:
        converter = lambda s: float(s.strip())
    
    # Load data
    if has_header:
        column_names = first_values
        data = np.loadtxt(file_path, delimiter=delimiter, skiprows=1, 
                         converters={i: converter for i in range(len(column_names))})
    else:
        # Load all data
        data = np.loadtxt(file_path, delimiter=delimiter,
                         converters={i: converter for i in range(len(first_values))})
        # Generate column names as numbers
        num_cols = data.shape[1] if data.ndim > 1 else 1
        column_names = [str(i) for i in range(num_cols)]
    
    # Ensure 2D array even for single column
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    
    return data, column_names

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

class ChnnelInfo:
    def __init__(self, unit: str, offset: float, gain: float):
        self.unit = unit
        self.offset = offset
        self.gain = gain

class TestDataSender:
    def __init__(self, ip=DEFAULT_IP, cmd_port=DEFAULT_CMD_PORT, data_port = None, local_port = 0, interval=0.001, print_interval=1.0, auto_enable = True):
        self.ip = ip
        self.cmd_port = cmd_port
        self.data_port = (DEFAULT_CMD_PORT + 1) if (not auto_enable) and data_port is None else data_port
        self.local_port = local_port
        self.interval = interval
        self.print_interval = print_interval
        self.num_signals = None
        self.sock = None
        self.channels = None
        self.test_mode_enabled = False if auto_enable else None


    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # Determine local interface that routes to target IP
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            temp_sock.connect((self.ip, self.cmd_port))
            local_ip = temp_sock.getsockname()[0]
        finally:
            temp_sock.close()
        
        self.sock.bind((local_ip, self.local_port))  # Bind to correct interface, random port
        self.sock.settimeout(1.0)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.sock:
            self.sock.close()
        return False

    def get_id(self) -> bool:
        data = self._send_cmd(CMD.GET_ID)
        if not data or len(data) < STRUCT.ID.size:
            return False
        id_info = STRUCT.ID.unpack(data[:STRUCT.ID.size])
        if id_info[0] != PACKET.ID_packet:
            print(f"Unexpected packet type: {id_info[0]}")
            return False
        crc = crc16_ccitt(data[:-STRUCT.CRC.size])
        recv_crc, = STRUCT.CRC.unpack(data[-STRUCT.CRC.size:])
        if crc != recv_crc:
            print(f"CRC mismatch: calculated {crc:04X}, received {recv_crc:04X}")
            return False
        self.channels = id_info[-1]
        self.channel_info = []
        for i in range(self.channels):
            offset = STRUCT.CHANNEL.size * i + STRUCT.ID.size
            channel_data = data[offset:offset+STRUCT.CHANNEL.size]
            if len(channel_data) < STRUCT.CHANNEL.size:
                print(f"Not enough data for channel {i}")
                return False
            unit, offset, gain = STRUCT.CHANNEL.unpack(channel_data)
            self.channel_info.append(ChnnelInfo(unit.decode('utf-8').rstrip('\x00'), offset, gain))
        return True

    def enable_test_mode(self) -> bool:
        port = b'' if self.data_port is None else STRUCT.PORT.pack(self.data_port)
        data = self._send_cmd_with_ack(CMD.ENABLE_TEST_MODE, port)
        if data is False:
            self.test_mode_enabled = False
            return False
        ack_info, data = data
        if not data or len(data) < STRUCT.PORT.size:
            self.test_mode_enabled = False
            return False
        port = STRUCT.PORT.unpack(data)[0]
        if port == 0:
            print(f"Device failed to open test data input port, error {ack_info}.")
            self.test_mode_enabled = False
            return False
        if self.data_port is not None and port != self.data_port:
            print(f"Warning: Device assigned data port {port}, expected {self.data_port}")
        self.data_port = port
        self.test_mode_enabled = True
        return True

    def disable_test_mode(self) -> bool:
        data = self._send_cmd_with_ack(CMD.DISABLE_TEST_MODE)
        if data is not False:
            self.test_mode_enabled = False
            return True
        return False

    def send_test_data(self, data_file: str, columns: list = [], raw: list | True = [], math: list = [], rounding: list = [], fill: float | None = None, samples: int = 0, offset: int = 0, clip_min: int = -32768, clip_max: int = 32767, packet_num: int = 0) -> bool:
        try:
            data, column_names = load_csv(data_file)
        except Exception as e:
            print(f"Failed to load CSV file: {e}")
            return False
        if self.channels is None:
            if not self.get_id():
                print("Failed to get device ID, cannot send test data.")
                return False
        if not columns:
            if data.shape[1] != self.channels:
                print(f"Data column count {data.shape[1]} does not match device channels {self.channels}, using first {self.channels} columns (could be specified by --column option).")
            columns = list(range(self.channels))
        else:
            if len(columns) != self.channels:
                print(f"Specified columns count {len(columns)} does not match device channels {self.channels}. Sending nothing.")
                return False
        for i, col in enumerate(columns):
            if isinstance(col, int):
                if col < 0 or col >= data.shape[1]:
                    print(f"Column index {col} out of range for data with {data.shape[1]} columns. Sending nothing.")
                    return False
            else:
                if col in column_names:
                    columns[i] = column_names.index(col)
                else:
                    print(f"Column '{col}' not found in data columns. Sending nothing.")
                    return False
        if raw is True:
            raw = columns
        else:
            for i, r in enumerate(raw):
                if not isinstance(r, int):
                    if r in column_names:
                        raw[i] = column_names.index(r)
                        if raw[i] not in columns:
                            print(f"Raw column '{r}' is not in specified columns {columns}. Sending nothing.")
                            return False
                    else:
                        print(f"Raw column '{r}' not found in data columns. Sending nothing.")
                        return False
        if self.test_mode_enabled is not None and not self.test_mode_enabled:
            if not self.enable_test_mode():
                print("Failed to enable test mode, cannot send test data.")
                return False
        if offset != 0:
            abs_offset = abs(offset)
            if abs_offset >= data.shape[0]:
                print(f"Offset {offset} is out of range for {data.shape[0]} samples. Sending nothing.")
                return False
            # Rotate data so that offset becomes the new start, preserving all rows for cyclic repetition.
            # Positive offset: start from row N. Negative offset: start from row N (from end).
            data = np.roll(data, -offset, axis=0)
        if samples > 0:
            if samples <= data.shape[0]:
                data = data[:samples]
            else:
                # Repeat rows cyclically from the start until requested sample count is reached.
                repeats = (samples + data.shape[0] - 1) // data.shape[0]
                data = np.tile(data, (repeats, 1))[:samples]
        elif samples < 0:
            requested = -samples
            if data.shape[0] == 0:
                print("Input CSV contains no samples.")
                return False
            # Start from the position that corresponds to taking samples from file end,
            # then continue cyclically while preserving original row order.
            start_idx = (-requested) % data.shape[0]
            idx = (np.arange(requested) + start_idx) % data.shape[0]
            data = data[idx]
        num_samples = data.shape[0]
        if (num_samples % SAMPLES_PER_PACKET) != 0:
            if fill is not None:
                padding = np.full((SAMPLES_PER_PACKET - (num_samples % SAMPLES_PER_PACKET), data.shape[1]), fill)
                data = np.vstack([data, padding])
            else:
                print(f"Warning: Number of samples {num_samples} is not a multiple of {SAMPLES_PER_PACKET}, trimming last {num_samples % SAMPLES_PER_PACKET} samples.")
                data = data[:-(num_samples % SAMPLES_PER_PACKET)]
            num_samples = data.shape[0]

        # Apply math transformations (data = data * gain + offset) if specified
        for math_entry in math:
            col_idx, gain, offset_val = math_entry
            if isinstance(col_idx, str):
                if col_idx in column_names:
                    col_idx = column_names.index(col_idx)
                else:
                    print(f"Math column '{col_idx}' not found in data columns. Skipping.")
                    continue
            if col_idx < 0 or col_idx >= data.shape[1]:
                print(f"Math column index {col_idx} out of range for data with {data.shape[1]} columns. Skipping.")
                continue
            data[:, col_idx] = data[:, col_idx] * gain + offset_val

        # to raw values
        for i in range(self.channels):
            ch_info = self.channel_info[i]
            if columns[i] not in raw:
                data[:, columns[i]] = (data[:, columns[i]] - ch_info.offset) / ch_info.gain
        # Apply rounding before clipping (per-column rounding method)
        for col_idx, method in rounding:
            if isinstance(col_idx, str):
                if col_idx in column_names:
                    col_idx = column_names.index(col_idx)
                else:
                    print(f"Rounding column '{col_idx}' not found in data columns. Skipping.")
                    continue
            if col_idx < 0 or col_idx >= data.shape[1]:
                print(f"Rounding column index {col_idx} out of range for data with {data.shape[1]} columns. Skipping.")
                continue
            if method.lower() == "floor":
                data[:, col_idx] = np.floor(data[:, col_idx])
            elif method.lower() == "ceil":
                data[:, col_idx] = np.ceil(data[:, col_idx])
            elif method.lower() == "round":
                data[:, col_idx] = np.round(data[:, col_idx])
            elif method.lower() == "truncate":
                data[:, col_idx] = np.trunc(data[:, col_idx])
            else:
                print(f"Unknown rounding method '{method}'. Skipping.")
        # trim dinamic range to int16 and counbt outlayers
        for i in range(self.channels):
            outliers = np.sum((data[:, columns[i]] < clip_min) | (data[:, columns[i]] > clip_max))
            if outliers > 0:
                print(f"Warning: Channel {i} has {outliers} outliers outside range ({clip_min}, {clip_max}) after conversion, which will be clipped.")
            data[:, columns[i]] = np.clip(data[:, columns[i]], clip_min, clip_max)
        # Build packets as [channel0 block, channel1 block, ...] for each SAMPLES_PER_PACKET rows.
        selected = data[:, columns]
        data = (
            selected
            .reshape(-1, SAMPLES_PER_PACKET, self.channels)
            .transpose(0, 2, 1)
            .reshape(-1, self.channels * SAMPLES_PER_PACKET)
            .astype(np.int16)
        )
        total_packets = data.shape[0]
        parity_errors = (self.channels + (self.channels & 1)) * b'\0'
        fault_state = STRUCT.FAULT_STATE.pack(0)
        start_time = time.monotonic()
        for i in range(total_packets):
            pkt = STRUCT.DATA_HEADER.pack(PACKET.DATA_packet, packet_num & 0xFFFF) \
                + data[i].tobytes() \
                + parity_errors \
                + fault_state
            crc = STRUCT.CRC.pack(crc16_ccitt(pkt))
            self.sock.sendto(pkt + crc, (self.ip, self.data_port))
            packet_num += 1
            if (i+1) % int(self.print_interval / self.interval) == 0:
                print(f"Sent packet {i+1}/{total_packets}")
            # Sleep until next packet time
            next_time = start_time + (i + 1) * self.interval
            sleep_time = next_time - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)

        return True
    
    def _send_cmd(self, cmd: CMD, data: bytes = b'', expect_response: bool = True) -> None | bytes:
        pkt = STRUCT.CMD.pack(cmd) + data
        self.sock.sendto(pkt, (self.ip, self.cmd_port))
        if not expect_response:
            return
        try:
            data, addr = self.sock.recvfrom(1024)   # buffer size 1 KB
            #print(f"Received {len(data)} bytes from {addr}: {data}")
            return data
        except socket.timeout:
            print("No response received (timeout).")
            return b''
        
    def _send_cmd_with_ack(self, cmd: CMD, data: bytes = b'') -> False | tuple[int, bytes]:
        data = self._send_cmd(cmd, data)
        if not data or len(data) < STRUCT.ACK.size:
            return False
        ack_info = STRUCT.ACK.unpack(data[:STRUCT.ACK.size])
        if ack_info[0] != PACKET.ACK_packet or ack_info[2] != cmd:
            print(f"Unexpected ACK packet: {ack_info}")
            return False
        return (ack_info[1], data[STRUCT.ACK.size:])

def main(args: list[str]) -> int:
    match len(args):
        case 3:
            args = [args[0], "--ip", args[1], args[2]]
    parser = argparse.ArgumentParser(description="Utility for sending test data packets to FDDS UI nodes.")
    parser.add_argument("-a", "--ip", type=str, default=DEFAULT_IP, 
            help="IP address of the target node (default: %(default)s)")
    parser.add_argument("-p", "--port", type=int, 
            help=f"Port number to send commands to (default: {DEFAULT_CMD_PORT})")
    parser.add_argument("-d", "--data_port", type=int, default=None, 
            help="Port number to send data to (default: got from device)")
    parser.add_argument("-l", "--local_port", type=int, default=0, 
            help=f"Port number to bind locally for sending commands (default: %(default)d, random if 0)")
    parser.add_argument("-i", "--interval", type=float, default=0.001, 
            help="Interval between sending data packets in seconds (default: %(default).3f)")
    parser.add_argument("-I", "--print_interval", type=float, default=1.0, 
            help="Interval for printing status updates in seconds (default: %(default).1f)")
    parser.add_argument("-c", "--column", action="append", default=[], help="Column name or number (indexed from 0) for each channel (can be specified multiple times)")
    parser.add_argument("-r", "--raw", action="append", default=[], help="Column name or number (indexed from 0) of channels containing raw data (can be specified multiple times)")
    parser.add_argument("-R", "--allraw", action="store_true", help="Treat all channels as raw data (overrides --raw)")
    parser.add_argument("-f", "--fill", type=float, default=None, help="Fill missing data with this value (default: trim last incomplete packet from input data)")
    parser.add_argument("-E", "--just_enable", action="store_true", help="Just enable test mode without sending data")
    parser.add_argument("-D", "--disable_test_mode", action="store_true", help="Just disable test mode")
    parser.add_argument("-S", "--skip_enable", action="store_true", help="Just send data, without explicit enabling and disabling test mode")
    parser.add_argument("-s", "--samples", type=int, default=0, help="Number of samples to send (default: 0=all)")
    parser.add_argument("-o", "--offset", type=int, default=0, help="Row offset to start reading from in CSV file (default: %(default)d)")
    parser.add_argument("-m", "--math", nargs=3, action="append", default=[], metavar=("COLUMN", "GAIN", "OFFSET"), help="Apply math transformation: data = data * gain + offset (can be specified multiple times)")
    parser.add_argument("-g", "--rounding", nargs=2, action="append", default=[], metavar=("COLUMN", "METHOD"), help="Rounding method per column: floor, ceil, round, truncate (default) (can be specified multiple times)")
    parser.add_argument("-C", "--clip-range", nargs=2, type=int, default=[-32768, 32767], metavar=("MIN", "MAX"), help="Clipping range for int16 conversion (default: -32768 32767)")
    parser.add_argument("-n", "--packet_num", type=int, default=0, help="Starting packet number (default: %(default)d)")
    parser.add_argument("data_file", nargs="?", type=str, default=None, help="Path to the file containing test data.")
    args = parser.parse_args(args[1:])
    if args.just_enable and args.disable_test_mode:
        print("Cannot use --just_enable and --disable_test_mode together.")
        return 1
    if args.skip_enable and (args.just_enable or args.disable_test_mode):
        print("Cannot use --skip_enable with --just_enable or --disable_test_mode.")
        return 1
    if (not (args.just_enable or args.disable_test_mode)) and args.data_file is None:
        print("Data file must be specified unless just enabling or disabling test mode.")
        return 1
    if args.port is None:
        addr, port = args.ip.split(":") if ":" in args.ip else (args.ip, DEFAULT_CMD_PORT)
        args.ip = addr
        try:
            args.port = int(port)
        except ValueError:
            args.port = DEFAULT_CMD_PORT
    for where in [args.column, args.raw]:
        if where:
            for i, val in enumerate(where):
                if val.isdigit():
                    where[i] = int(val)
    if args.allraw:
        args.raw = args.column if args.column else True
    # Parse math argument: convert column to index (if digit) and gains/offsets to float
    math_parsed = []
    for math_entry in args.math:
        col, gain, offset_val = math_entry
        try:
            col = int(col) if col.isdigit() else col
            gain = float(gain)
            offset_val = float(offset_val)
            math_parsed.append((col, gain, offset_val))
        except (ValueError, IndexError) as e:
            print(f"Invalid math argument: {math_entry}. Error: {e}")
            return 1
    args.math = math_parsed
    # Parse rounding argument: convert column to index (if digit) and validate method
    rounding_parsed = []
    for rounding_entry in args.rounding:
        col, method = rounding_entry
        try:
            col = int(col) if col.isdigit() else col
            if method.lower() not in ["floor", "ceil", "round", "truncate"]:
                print(f"Invalid rounding method '{method}'. Allowed: floor, ceil, round, truncate.")
                return 1
            rounding_parsed.append((col, method))
        except (ValueError, IndexError) as e:
            print(f"Invalid rounding argument: {rounding_entry}. Error: {e}")
            return 1
    args.rounding = rounding_parsed
    # Parse and validate clip-range arguments
    clip_min, clip_max = args.clip_range
    if clip_min > clip_max:
        print(f"Clip min ({clip_min}) cannot be greater than clip max ({clip_max}).")
        return 1
    if clip_min < -32768 or clip_max > 32767:
        print(f"Clip range ({clip_min}, {clip_max}) exceeds int16 range (-32768, 32767).")
        return 1
    args.clip_min = clip_min
    args.clip_max = clip_max
    with TestDataSender(ip=args.ip, cmd_port=args.port, data_port=args.data_port, interval=args.interval, print_interval=args.print_interval, auto_enable=not args.skip_enable) as sender:
        if not (args.just_enable or args.disable_test_mode):
            if not sender.get_id():
                print("Failed to get device ID. Exiting.")
                return 1
            else:
                print(f"Device has {sender.channels} channels:")
                for i, ch_info in enumerate(sender.channel_info):
                    print(f"  Channel {i}: unit='{ch_info.unit}', offset={ch_info.offset}, gain={ch_info.gain}")
        if not (args.disable_test_mode or args.skip_enable):
            if not sender.enable_test_mode():
                print("Failed to enable test mode. Exiting.")
                return 1
            else:
                print("Test mode enabled.")
        res = 0
        if not (args.just_enable or args.disable_test_mode):
            if not sender.send_test_data(args.data_file, args.column, args.raw, args.math, args.rounding, args.fill, args.samples, args.offset, args.clip_min, args.clip_max, args.packet_num):
                print("Failed to send test data.")
                res = 1
            else:
                print("Data sent.")
        if not (args.just_enable or args.skip_enable):
            if not sender.disable_test_mode():
                print("Failed to disable test mode.")
                res = 1
            else:
                print("Test mode disabled.")
        return res

if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))

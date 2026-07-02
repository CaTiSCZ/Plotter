"""
FDDS protocol definitions — packet/command enums, struct layouts, port constants.

Mirrors the C definitions in App/communication/protocol.h and comm_common.h.
"""

import struct
from enum import IntEnum, unique

# ---------------------------------------------------------------------------
# Port assignments (from comm_common.h)
# ---------------------------------------------------------------------------

UDP_DBG_PORT = 10577
UDP_CMD_PORT = 10578
UDP_BOOT_PORT = 10579
UDP_DATA_PORT = 10580

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

SAVE_KEY = 0xAC
RESET_KEY = 0xFE
MAX_RECEIVERS = 4
SAMPLES_PER_PACKET = 200
GATHERING_DEVICES = 4
ACQUISITION_CHANNELS = 2
CALIBRATION_INFO_DATA_SIZE = 12

# ---------------------------------------------------------------------------
# Algorithm-config (CMD_*_ALG_CONFIG) constants — mirror App/communication/alg_config.h
# ---------------------------------------------------------------------------

ALG_CONFIG_TAG_NODE = 0x4E44  # 'ND'
ALG_CONFIG_TAG_CCU = 0x4343   # 'CC'
NODE_ALG_SCHEMA_VER = 1
CCU_ALG_SCHEMA_VER = 1

# Section ids
ALG_SEC_ALL = 0xFF
ALG_SEC_NODE_SC = 0x01       # sc_b, sc_a, sc_g, sc_scales, sc_weights_w, sc_weights_b
ALG_SEC_NODE_DV_FILT = 0x02  # dv_refSourceVoltage, dv_maxAmplDeviation, dv_disturbanceTime
ALG_SEC_NODE_DV_CURV = 0x03  # dvc_upper/lower bound voltage+time
ALG_SEC_CCU_ARC = 0x10       # arc_* matrices + arc_flagBias
ALG_SEC_CCU_SC = 0x11        # sc_a, sc_i0, sc_threshold
ALG_SEC_CCU_DS = 0x12        # ds_z1..z3, ds_id_length

# ---------------------------------------------------------------------------
# Enum helpers
# ---------------------------------------------------------------------------


class IntEnumName(IntEnum):
    """IntEnum subclass whose __str__ returns the member name."""
    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Packet types (packet_type_t)
# ---------------------------------------------------------------------------

@unique
class PACKET(IntEnumName):
    ACK = 0
    ID = 1
    DATA = 2
    TRIGGER = 3
    LOG = 4
    RESULT = 5
    DS_RESULT = 6
    SAMPLE_RESULT = 7
    CCU_PHYSICAL = 8


# ---------------------------------------------------------------------------
# Commands (cmd_t)
# ---------------------------------------------------------------------------

@unique
class CMD(IntEnumName):
    PING = 0
    GET_ID = 1
    REGISTER_RECEIVER = 2
    REMOVE_RECEIVER = 3
    GET_RECEIVERS = 4
    START_SAMPLING = 5
    START_ON_TRIGGER = 6
    STOP_SAMPLING = 7
    TRIGGER_ACK = 8
    FORCE_TRIGGER = 9
    RESET_COUNTERS = 10
    CLOCK_CONFIG = 11
    GET_ACQUISITION_STATE = 12
    DEVICE_ID_CONFIG = 13
    RESET_DEVICE = 14
    GET_CLOCK_CONFIG = 15
    FW_UPDATE_REQUEST = 16
    SET_CALIBRATION = 17
    ENABLE_TEST_DATA_INPUT = 18
    DISABLE_TEST_DATA_INPUT = 19
    WAIT_TRIGGER_PTP = 20
    RESET_FAULT_STATE = 21
    HW_CONFIG = 22
    GET_SYSTEM_STATE = 23
    STARTUP_CONTROL = 24
    STOP_SYSTEM = 25
    SET_NODE_IPS = 26
    GET_CCU_CALIBRATION = 27
    SET_CCU_CALIBRATION = 28
    GET_FW_ID = 29
    GET_NET_CONFIG = 30
    SET_NET_CONFIG = 31
    GET_CALIBRATION = 32
    START_SAMPLING_DEFERRED = 33
    GET_PTP_TIME = 34
    BUSYWAIT = 35
    SET_TRIGGER_CONFIG = 36
    GET_TRIGGER_CONFIG = 37
    RESEND_PACKET = 38
    GET_DS_STATE = 39
    GET_CCU_PHYSICAL = 40
    RESET_ALGORITHMS = 41
    GET_NODE_IPS = 42
    TEST_CRC_FAULT = 43
    GET_LOOP_STATS = 44
    SET_DS_IMPEDANCE = 45        # DEPRECATED — use SET_ALG_CONFIG (section CCU_DS)
    SAVE_DS_IMPEDANCE = 46       # DEPRECATED — use SAVE_ALG_CONFIG
    FORCE_DS_IDENTIFICATION = 47
    GET_DS_SAVED_IMPEDANCE = 48  # DEPRECATED — use GET_ALG_CONFIG_SAVED (section CCU_DS)
    GET_ALG_CONFIG = 49
    SET_ALG_CONFIG = 50
    GET_ALG_CONFIG_SAVED = 51
    SAVE_ALG_CONFIG = 52
    GET_CHANNEL_MINMAX = 53
    RESET_CHANNEL_MINMAX = 54
    GPIO_DIAG = 55
    PING_NODE = 56
    PING_NODE_ICMP = 57


# ---------------------------------------------------------------------------
# Receiver type indices (used by REGISTER_RECEIVER / REMOVE_RECEIVER)
# ---------------------------------------------------------------------------

RECEIVER_TYPE_DATA = 0
RECEIVER_TYPE_LOG = 1
RECEIVER_TYPE_DBG = 2

RECEIVER_TYPE_MAP = {"data": 0, "log": 1, "debug": 2, "dbg": 2}
RECEIVER_TYPE_NAMES = {0: "Data", 1: "Log", 2: "Debug"}
RECEIVER_DEFAULT_PORTS = {0: UDP_DATA_PORT, 1: UDP_CMD_PORT, 2: UDP_DBG_PORT}


# ---------------------------------------------------------------------------
# Struct layouts (little-endian, matching firmware packing)
# ---------------------------------------------------------------------------

class STRUCT:
    """Binary struct formats for FDDS protocol packets."""
    CMD = struct.Struct("<I")
    ACK = struct.Struct("<HHI")             # packet_type, state, cmd
    ID_V4 = struct.Struct("<HH HBB HBBI3I HBBI HH") # last HH = channels_count + _reserved
    ID_V5 = struct.Struct("<HH HBB HBBI3I HBBI HBB") # channels_count + fault counts
    ID = ID_V4
    CHANNEL = struct.Struct("<4s ff")       # unit(4 bytes), offset, gain
    CRC = struct.Struct("<H")
    FW_INFO = struct.Struct("<HBB I 8s 30s 48s BB H 2x I I")  # fw_info_t (108 bytes)
    NET_INFO = struct.Struct("<6s 2s I I I")               # net_info_t (20 bytes)
    CALIBRATION_INFO = struct.Struct("<Q I")               # calibration_info_t (12 bytes)
    PORT = struct.Struct("<H")
    DATA_HEADER = struct.Struct("<HHII")    # packet_type, packet_num, ptp_seconds, ptp_nanoseconds
    FAULT_STATE = struct.Struct("<H")
    RESULT_HEADER = struct.Struct("<HH")    # packet_type, packet_num
    RESULT_VALUE = struct.Struct("<H")      # value
    LOG_HEADER = struct.Struct("<HH")       # packet_type, msg_counter
    DS_STATE = struct.Struct("<HBBfffHH")  # frame_count, phase, reserved, z1,z2,z3,ims_threshold,id_len,reserved2
    # CMD_PING_NODE (56) — on-device RTT measurement
    PING_RTT_REQUEST = struct.Struct("<BBIHHBB")  # addr_mode, node_index, target_ip, count, interval_ms, silent, target_silent (12 B)
    PING_RTT_RESULT = struct.Struct("<IIHHQQQ")  # origin_ip, target_ip, count_sent, count_recv, min, avg, max (36 B)
    PING_RTT_SAMPLE = struct.Struct("<I")        # per-ping RTT in ns (0xFFFFFFFF = lost)


# ---------------------------------------------------------------------------
# Packet size helpers
# ---------------------------------------------------------------------------

ACK_PACKET_HEADER_SIZE = 8  # packet_type(2) + state(2) + cmd(4)
LOG_PACKET_HEADER_SIZE = 4  # packet_type(2) + msg_counter(2)


def data_packet_size(channels: int, samples: int = SAMPLES_PER_PACKET,
                     new_format: bool = False) -> int:
    """Calculate data_packet_t wire size for given channel/sample count."""
    padding = channels % 2
    return (
        2                           # packet_type
        + 2                         # packet_num
        + 4                         # ptp_seconds
        + 4                         # ptp_nanoseconds
        + channels * samples * 2    # int16 data[channels][samples]
        + channels                  # parity_errors[channels]
        + padding                   # padding for alignment
        + 2                         # fault_state
        + (2 if new_format else 0)  # fault_latched (FW v5+)
        + 2                         # crc
    )


def result_packet_size(gathering_devices: int = GATHERING_DEVICES,
                       channels: int = ACQUISITION_CHANNELS,
                       new_format: bool = False) -> int:
    """Calculate result_packet_t wire size."""
    return (
        2                                   # packet_type
        + 2                                 # packet_num
        + 4                                 # ptp_seconds
        + 4                                 # ptp_nanoseconds
        + 2                                 # value
        + gathering_devices * 2             # fault_state[GATHERING_DEVICES] (uint16 per node)
        + (gathering_devices * 2 if new_format else 0)  # fault_latched[GATHERING_DEVICES] (FW v5+)
        + gathering_devices * channels      # parity_errors[GATHERING_DEVICES][ACQUISITION_CHANNELS]
        + 1                                 # crc_error_mask (bit per node)
        + 1                                 # missing_mask (bit per node)
        + 2                                 # crc
    )


# ---------------------------------------------------------------------------
# DS Result packet (PACKET_DS_RESULT = 6)
# Carries one line segment of digital shadow deviation_U per packet.
# 3 packets (segment 0,1,2) form one complete window.
# Layout: packet_type(2) + packet_num(2) + segment(1) + padding(3)
#          + deviation[SAMPLES_PER_PACKET](float32) + crc(2)
# Total: 810 bytes per segment packet.
# ---------------------------------------------------------------------------

LINE_SEGMENTS = 3
DS_RESULT_HEADER = struct.Struct("<HH B 3x")  # packet_type, packet_num, segment, pad[3]
DS_RESULT_HEADER_SIZE = DS_RESULT_HEADER.size  # 8 bytes


def ds_result_packet_size(samples: int = SAMPLES_PER_PACKET) -> int:
    """Calculate ds_result_packet_t wire size (one segment)."""
    return (
        DS_RESULT_HEADER_SIZE       # 8: type(2) + num(2) + segment(1) + pad(3)
        + samples * 4               # float32[samples]
        + 2                         # crc
    )


# ---------------------------------------------------------------------------
# CCU Physical packet (PACKET_CCU_PHYSICAL = 8)
# Carries one physical signal segment per packet (segment 0..7).
# Layout mirrors DS result packet: packet_type(2)+packet_num(2)+segment(1)+pad(3)
#          + values[SAMPLES_PER_PACKET](float32) + crc(2)
# ---------------------------------------------------------------------------

CCU_PHYSICAL_SIGNALS = 8
CCU_PHYSICAL_HEADER = struct.Struct("<HH B 3x")
CCU_PHYSICAL_HEADER_SIZE = CCU_PHYSICAL_HEADER.size  # 8 bytes


def ccu_physical_packet_size(samples: int = SAMPLES_PER_PACKET) -> int:
    """Calculate ccu_physical_packet_t wire size (one signal segment)."""
    return (
        CCU_PHYSICAL_HEADER_SIZE
        + samples * 4
        + 2
    )


# ---------------------------------------------------------------------------
# Sample Result packet (PACKET_SAMPLE_RESULT = 7)
# Per-sample computation results from node (test mode only).
# Layout: packet_type(2) + packet_num(2) + results[SAMPLES_PER_PACKET](uint16) + crc(2)
# Total: 406 bytes.
# ---------------------------------------------------------------------------

SAMPLE_RESULT_HEADER = struct.Struct("<HH")  # packet_type, packet_num


def sample_result_packet_size(samples: int = SAMPLES_PER_PACKET) -> int:
    """Calculate sample_result_packet_t wire size."""
    return (
        4                           # packet_type(2) + packet_num(2)
        + samples * 2               # uint16[samples]
        + 2                         # crc
    )

"""
FDDS shared utilities package.

Provides protocol definitions, CRC, CSV loading, and device connection classes
shared across all FDDS Python tools.
"""

from .crc import crc16_ccitt
from .protocol import (
    PACKET, CMD, STRUCT, SAMPLES_PER_PACKET,
    UDP_CMD_PORT, UDP_DATA_PORT, UDP_DBG_PORT, UDP_BOOT_PORT,
    SAVE_KEY, RESET_KEY, MAX_RECEIVERS,
)
from .csv_loader import load_csv

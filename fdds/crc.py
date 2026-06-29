"""CRC-16/CCITT checksum used by the FDDS protocol."""

try:
    import crcmod as _crcmod
    _crc16_fn = _crcmod.mkCrcFun(0x11021, initCrc=0xFFFF, rev=False)

    def crc16_ccitt(data: bytes, poly: int = 0x1021, crc: int = 0xFFFF) -> int:
        """Calculate CRC-16/CCITT over *data* (fast C implementation)."""
        if crc == 0xFFFF and poly == 0x1021:
            return _crc16_fn(data)
        # Fallback for non-standard parameters
        return _crc16_ccitt_pure(data, poly, crc)

except ImportError:
    import warnings
    warnings.warn(
        "crcmod not installed — using slow pure-Python CRC16. "
        "Install with: pip install crcmod",
        stacklevel=2,
    )


def _crc16_ccitt_pure(data: bytes, poly: int = 0x1021, crc: int = 0xFFFF) -> int:
    """Pure-Python fallback for CRC-16/CCITT."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


try:
    crc16_ccitt
except NameError:
    crc16_ccitt = _crc16_ccitt_pure

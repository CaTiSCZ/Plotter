"""
Transport abstraction for FDDS device commands.

A :class:`Transport` carries a raw command payload (``cmd(4 LE) + data``) to a
device and returns the raw response bytes (the same ``ACK`` packet a UDP reply
would contain). This lets :class:`fdds.connection.DeviceConnection` issue
provisioning/diagnostic commands over three channels:

* :class:`UdpTransport`        — normal unicast UDP (default).
* :class:`BroadcastTransport`  — UDP broadcast (IP bootstrap, may see N replies).
* :class:`SerialTransport`     — USART3 VCP binary console (works with no IP).

The serial framing matches the firmware ``serial_console`` binary protocol:

    SOF0=0x7E SOF1=0xA5 | VER/FLAGS | SEQ | LEN(u16 LE) | PAYLOAD | CRC16(u16 LE)

VER/FLAGS: high nibble = version (1), low nibble bit0 = is_reply, bit1 = is_nack.
CRC-16/CCITT (poly 0x1021, init 0xFFFF) is computed over VER..PAYLOAD.
"""

import socket
import struct
from typing import Optional

from .crc import crc16_ccitt
from .protocol import UDP_CMD_PORT


# ---------------------------------------------------------------------------
# Serial frame constants (must match App/communication/serial_console.cpp)
# ---------------------------------------------------------------------------

SERIAL_SOF0 = 0x7E
SERIAL_SOF1 = 0xA5
SERIAL_VERSION = 1
SERIAL_FLAG_REPLY = 0x01
SERIAL_FLAG_NACK = 0x02
SERIAL_MAX_PAYLOAD = 1024


class TransportError(Exception):
    """Raised on an unrecoverable transport failure."""


class Transport:
    """Abstract command transport.

    Subclasses carry a raw ``payload`` (cmd+data) and return the device's raw
    response bytes, or ``None`` on timeout.
    """

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def request(self, payload: bytes, timeout: float) -> Optional[bytes]:
        """Send *payload* and return the response bytes (or ``None`` on timeout)."""
        raise NotImplementedError

    def send(self, payload: bytes) -> None:
        """Fire-and-forget send (no reply expected)."""
        raise NotImplementedError

    @property
    def local_ip(self) -> str:
        return ""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# UDP unicast
# ---------------------------------------------------------------------------

class UdpTransport(Transport):
    """Plain unicast UDP transport (the default channel)."""

    def __init__(self, ip: str, port: int = UDP_CMD_PORT,
                 timeout: float = 2.0, local_port: int = 0):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.local_port = local_port
        self.sock: Optional[socket.socket] = None
        self._local_ip = ""

    def open(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tmp.connect((self.ip, self.port))
            self._local_ip = tmp.getsockname()[0]
        finally:
            tmp.close()
        self.sock.bind((self._local_ip, self.local_port))
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    @property
    def local_ip(self) -> str:
        return self._local_ip

    def request(self, payload: bytes, timeout: float) -> Optional[bytes]:
        old = self.sock.gettimeout()
        try:
            self.sock.settimeout(timeout)
            self.sock.sendto(payload, (self.ip, self.port))
            resp, _addr = self.sock.recvfrom(4096)
            return resp
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(old)

    def send(self, payload: bytes) -> None:
        self.sock.sendto(payload, (self.ip, self.port))


# ---------------------------------------------------------------------------
# UDP broadcast (IP bootstrap)
# ---------------------------------------------------------------------------

class BroadcastTransport(Transport):
    """UDP broadcast transport for devices with an unknown / unreachable IP.

    A blank-EEPROM device falls back to the firmware default subnet, so its IP is
    not routable from the developer's network. Broadcasting to the local subnet
    (or 255.255.255.255) reaches it regardless. Because several devices may answer
    a broadcast, :meth:`request` returns only the first reply; use
    :meth:`request_all` to collect every reply within the timeout window.
    """

    def __init__(self, port: int = UDP_CMD_PORT, timeout: float = 2.0,
                 broadcast_addr: str = "255.255.255.255", local_port: int = 0):
        self.port = port
        self.timeout = timeout
        self.broadcast_addr = broadcast_addr
        self.local_port = local_port
        self.sock: Optional[socket.socket] = None

    def open(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        self.sock.bind(("", self.local_port))
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def request(self, payload: bytes, timeout: float) -> Optional[bytes]:
        old = self.sock.gettimeout()
        try:
            self.sock.settimeout(timeout)
            self.sock.sendto(payload, (self.broadcast_addr, self.port))
            resp, _addr = self.sock.recvfrom(4096)
            return resp
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(old)

    def request_all(self, payload: bytes, timeout: float) -> list[tuple[str, bytes]]:
        """Broadcast *payload* and collect every ``(ip, response)`` until *timeout*."""
        import time
        replies: list[tuple[str, bytes]] = []
        self.sock.sendto(payload, (self.broadcast_addr, self.port))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.sock.settimeout(remaining)
                resp, addr = self.sock.recvfrom(4096)
                replies.append((addr[0], resp))
            except socket.timeout:
                break
        return replies

    def send(self, payload: bytes) -> None:
        self.sock.sendto(payload, (self.broadcast_addr, self.port))


# ---------------------------------------------------------------------------
# Serial (USART3 VCP) binary console
# ---------------------------------------------------------------------------

class SerialTransport(Transport):
    """Binary command transport over the device's USART3 ST-Link VCP.

    Works even when the device has no usable IP (blank EEPROM). The device's
    debug log shares the same UART, so :meth:`request` discards any bytes that are
    not part of a well-formed reply frame for the expected sequence number.
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._seq = 0
        self.ser = None

    def open(self) -> None:
        try:
            import serial  # pyserial
        except ImportError as exc:
            raise TransportError(
                "pyserial is required for SerialTransport — install with "
                "'pip install pyserial' (or 'pip install -r requirements.txt')"
            ) from exc
        # A URL-style port (e.g. 'socket://host:port', 'rfc2217://...') needs the
        # URL handler; a plain device name (COM5, /dev/ttyACM0) opens directly.
        if "://" in self.port:
            self.ser = serial.serial_for_url(
                self.port, baudrate=self.baud, timeout=0.05)
        else:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.05)

    def close(self) -> None:
        if self.ser:
            self.ser.close()
            self.ser = None

    @staticmethod
    def _build_frame(seq: int, payload: bytes, flags: int = 0) -> bytes:
        if len(payload) > SERIAL_MAX_PAYLOAD:
            raise TransportError(f"payload too large: {len(payload)} > {SERIAL_MAX_PAYLOAD}")
        ver_flags = (SERIAL_VERSION << 4) | (flags & 0x0F)
        body = struct.pack("<BBH", ver_flags, seq & 0xFF, len(payload)) + payload
        crc = crc16_ccitt(body)
        return bytes((SERIAL_SOF0, SERIAL_SOF1)) + body + struct.pack("<H", crc)

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def request(self, payload: bytes, timeout: float) -> Optional[bytes]:
        import time
        seq = self._next_seq()
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(self._build_frame(seq, payload))
        self.ser.flush()

        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf.extend(chunk)
                result = self._scan_for_reply(buf, seq)
                if result is not None:
                    return result
        return None

    def send(self, payload: bytes) -> None:
        seq = self._next_seq()
        self.ser.write(self._build_frame(seq, payload))
        self.ser.flush()

    @staticmethod
    def _scan_for_reply(buf: bytearray, want_seq: int) -> Optional[bytes]:
        """Scan *buf* for a reply frame matching *want_seq*.

        Consumes parsed/garbage bytes from the front of *buf* in place. Returns
        the frame payload on a CRC-valid reply with the wanted sequence number,
        otherwise ``None`` (waiting for more bytes). Debug log text (anything not
        a valid frame) is skipped.
        """
        while True:
            # Find the next start-of-frame marker.
            start = -1
            for i in range(len(buf) - 1):
                if buf[i] == SERIAL_SOF0 and buf[i + 1] == SERIAL_SOF1:
                    start = i
                    break
            if start < 0:
                # Keep at most the last byte (could be a lone 0x7E).
                if len(buf) > 1:
                    del buf[:-1]
                return None
            if start > 0:
                del buf[:start]

            # Need header: SOF0 SOF1 VER SEQ LEN(2).
            if len(buf) < 6:
                return None
            length = buf[4] | (buf[5] << 8)
            if length > SERIAL_MAX_PAYLOAD:
                # Bogus length — drop this SOF and resync.
                del buf[:2]
                continue
            frame_len = 6 + length + 2
            if len(buf) < frame_len:
                return None  # wait for the rest

            body = bytes(buf[2:6 + length])           # VER..PAYLOAD
            rx_crc = buf[6 + length] | (buf[7 + length] << 8)
            ver_flags = buf[2]
            seq = buf[3]
            payload = bytes(buf[6:6 + length])

            del buf[:frame_len]                        # consume the frame

            if crc16_ccitt(body) != rx_crc:
                continue                               # corrupt, resync
            if (ver_flags >> 4) != SERIAL_VERSION:
                continue
            if not (ver_flags & SERIAL_FLAG_REPLY):
                continue
            if seq != (want_seq & 0xFF):
                continue
            return payload

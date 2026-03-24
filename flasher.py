#!/usr/bin/env python3
import argparse
import socket
import struct
import sys
import time
import zlib
from pathlib import Path


BOOT_PROTO_MAGIC = 0x544F4F42  # 'BOOT'

BOOT_CMD_START = 1
BOOT_CMD_START_ACK = 2
BOOT_CMD_DATA = 3
BOOT_CMD_DATA_ACK = 4
BOOT_CMD_FINISH = 5
BOOT_CMD_FINISH_ACK = 6
BOOT_CMD_ABORT = 7
BOOT_CMD_STATUS = 8
BOOT_CMD_NACK = 0x7F

BOOT_ERR_NONE = 0
BOOT_ERR_BAD_MAGIC = 1
BOOT_ERR_BAD_LENGTH = 2
BOOT_ERR_BAD_STATE = 3
BOOT_ERR_FLASH = 4
BOOT_ERR_CRC = 5
BOOT_ERR_RANGE = 6
BOOT_ERR_SEQUENCE = 7
BOOT_ERR_VERIFY = 8
BOOT_ERR_BOOTSEL = 9

ERR_NAMES = {
    BOOT_ERR_NONE: "NONE",
    BOOT_ERR_BAD_MAGIC: "BAD_MAGIC",
    BOOT_ERR_BAD_LENGTH: "BAD_LENGTH",
    BOOT_ERR_BAD_STATE: "BAD_STATE",
    BOOT_ERR_FLASH: "FLASH",
    BOOT_ERR_CRC: "CRC",
    BOOT_ERR_RANGE: "RANGE",
    BOOT_ERR_SEQUENCE: "SEQUENCE",
    BOOT_ERR_VERIFY: "VERIFY",
    BOOT_ERR_BOOTSEL: "BOOTSEL",
}

HDR_FMT = "<IBBH I"       # magic, cmd, reserved0, payload_len, msg_crc32
HDR_SIZE = struct.calcsize(HDR_FMT)

START_REQ_FMT = "<IIII"   # total_size, image_crc32, version, reserved
START_REQ_SIZE = struct.calcsize(START_REQ_FMT)

START_ACK_FMT = "<BBHI I"  # accepted, reserved0, chunk_max, expected_size, inactive_logical_base
START_ACK_SIZE = struct.calcsize(START_ACK_FMT)

DATA_PREFIX_FMT = "<IHHI"  # offset, data_len, reserved, data_crc32
DATA_PREFIX_SIZE = struct.calcsize(DATA_PREFIX_FMT)

DATA_ACK_FMT = "<I"
DATA_ACK_SIZE = struct.calcsize(DATA_ACK_FMT)

FINISH_REQ_FMT = "<II"
FINISH_REQ_SIZE = struct.calcsize(FINISH_REQ_FMT)

FINISH_ACK_FMT = "<BBHI"   # ok, reserved0, reserved1, detail
FINISH_ACK_SIZE = struct.calcsize(FINISH_ACK_FMT)

NACK_FMT = "<BBHI"         # error, reserved0, reserved1, detail
NACK_SIZE = struct.calcsize(NACK_FMT)


def crc32_bytes(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_message(cmd: int, payload: bytes) -> bytes:
    header_wo_crc = struct.pack(HDR_FMT, BOOT_PROTO_MAGIC, cmd, 0, len(payload), 0)
    msg_crc = crc32_bytes(header_wo_crc + payload)
    header = struct.pack(HDR_FMT, BOOT_PROTO_MAGIC, cmd, 0, len(payload), msg_crc)
    return header + payload


def parse_header(data: bytes):
    if len(data) < HDR_SIZE:
        raise ValueError("packet too short for header")
    magic, cmd, reserved0, payload_len, msg_crc32 = struct.unpack(HDR_FMT, data[:HDR_SIZE])
    return {
        "magic": magic,
        "cmd": cmd,
        "reserved0": reserved0,
        "payload_len": payload_len,
        "msg_crc32": msg_crc32,
    }


def validate_message(data: bytes):
    hdr = parse_header(data)
    if hdr["magic"] != BOOT_PROTO_MAGIC:
        raise ValueError(f"bad magic: 0x{hdr['magic']:08X}")
    if len(data) != HDR_SIZE + hdr["payload_len"]:
        raise ValueError(f"bad length: hdr says {hdr['payload_len']}, actual payload {len(data) - HDR_SIZE}")

    zero_crc_header = struct.pack(
        HDR_FMT,
        hdr["magic"],
        hdr["cmd"],
        hdr["reserved0"],
        hdr["payload_len"],
        0,
    )
    calc_crc = crc32_bytes(zero_crc_header + data[HDR_SIZE:])
    if calc_crc != hdr["msg_crc32"]:
        raise ValueError(f"bad packet crc: expected 0x{hdr['msg_crc32']:08X}, got 0x{calc_crc:08X}")

    return hdr, data[HDR_SIZE:]


class UdpBootFlasher:
    def __init__(
        self,
        ip: str,
        port: int,
        timeout: float,
        retries: int,
        chunk_size: int,
        verbose: bool,
    ):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.chunk_size = chunk_size
        self.verbose = verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.ip, self.port))

    def close(self):
        self.sock.close()

    def log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def recv_message(self):
        data = self.sock.recv(4096)
        hdr, payload = validate_message(data)
        return hdr, payload

    def request(self, cmd: int, payload: bytes, expect_cmds):
        packet = build_message(cmd, payload)

        for attempt in range(1, self.retries + 1):
            try:
                self.sock.send(packet)
                hdr, resp_payload = self.recv_message()

                if hdr["cmd"] == BOOT_CMD_NACK:
                    if len(resp_payload) != NACK_SIZE:
                        raise RuntimeError(f"NACK payload wrong size: {len(resp_payload)}")
                    error, _r0, _r1, detail = struct.unpack(NACK_FMT, resp_payload)
                    raise RuntimeError(
                        f"Target NACK: error={ERR_NAMES.get(error, error)} ({error}), detail=0x{detail:08X}"
                    )

                if hdr["cmd"] not in expect_cmds:
                    raise RuntimeError(f"Unexpected response cmd={hdr['cmd']}, expected one of {expect_cmds}")

                return hdr, resp_payload

            except socket.timeout:
                self.log(f"Timeout waiting for response to cmd {cmd}, retry {attempt}/{self.retries}")
                if attempt == self.retries:
                    raise RuntimeError(f"Timeout waiting for response to cmd {cmd}")
            except ValueError as e:
                self.log(f"Invalid response packet: {e}, retry {attempt}/{self.retries}")
                if attempt == self.retries:
                    raise RuntimeError(f"Invalid response packet after retries: {e}")

        raise RuntimeError("unreachable")

    def send_start(self, image_size: int, image_crc32: int, version: int):
        payload = struct.pack(
            START_REQ_FMT,
            image_size,
            image_crc32,
            version,
            0,
        )
        _, resp_payload = self.request(BOOT_CMD_START, payload, {BOOT_CMD_START_ACK})

        if len(resp_payload) != START_ACK_SIZE:
            raise RuntimeError(f"START_ACK payload wrong size: {len(resp_payload)}")

        accepted, _r0, chunk_max, expected_size, inactive_logical_base = struct.unpack(
            START_ACK_FMT,
            resp_payload,
        )

        if accepted != 1:
            raise RuntimeError("Target rejected START")

        return {
            "chunk_max": chunk_max,
            "expected_size": expected_size,
            "inactive_logical_base": inactive_logical_base,
        }

    def send_data_chunk(self, offset: int, chunk: bytes):
        payload = struct.pack(
            DATA_PREFIX_FMT,
            offset,
            len(chunk),
            0,
            crc32_bytes(chunk),
        ) + chunk

        _, resp_payload = self.request(BOOT_CMD_DATA, payload, {BOOT_CMD_DATA_ACK})

        if len(resp_payload) != DATA_ACK_SIZE:
            raise RuntimeError(f"DATA_ACK payload wrong size: {len(resp_payload)}")

        (next_offset,) = struct.unpack(DATA_ACK_FMT, resp_payload)
        return next_offset

    def send_finish(self, image_size: int, image_crc32: int):
        payload = struct.pack(FINISH_REQ_FMT, image_size, image_crc32)
        _, resp_payload = self.request(BOOT_CMD_FINISH, payload, {BOOT_CMD_FINISH_ACK})

        if len(resp_payload) != FINISH_ACK_SIZE:
            raise RuntimeError(f"FINISH_ACK payload wrong size: {len(resp_payload)}")

        ok, _r0, _r1, detail = struct.unpack(FINISH_ACK_FMT, resp_payload)
        return {"ok": ok, "detail": detail}

    def send_abort(self):
        try:
            self.request(BOOT_CMD_ABORT, b"", {BOOT_CMD_FINISH_ACK})
        except Exception:
            pass


def print_progress(done: int, total: int, start_time: float):
    width = 40
    frac = 0.0 if total == 0 else done / total
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    elapsed = max(time.time() - start_time, 1e-6)
    speed = done / elapsed
    print(
        f"\r[{bar}] {done}/{total} bytes  {frac * 100:6.2f}%  {speed/1024:8.1f} KiB/s",
        end="",
        flush=True,
    )


def flash_image(args) -> int:
    image_path = Path(args.bin)
    if not image_path.is_file():
        print(f"File not found: {image_path}", file=sys.stderr)
        return 2

    image = image_path.read_bytes()
    image_size = len(image)
    image_crc = crc32_bytes(image)

    if image_size == 0:
        print("Refusing to flash empty file", file=sys.stderr)
        return 2

    print(f"Target          : {args.ip}:{args.port}")
    print(f"Image           : {image_path}")
    print(f"Image size      : {image_size} bytes")
    print(f"Image CRC32     : 0x{image_crc:08X}")
    print(f"Version         : {args.version}")

    flasher = UdpBootFlasher(
        ip=args.ip,
        port=args.port,
        timeout=args.timeout,
        retries=args.retries,
        chunk_size=args.chunk_size,
        verbose=args.verbose,
    )

    try:
        start_info = flasher.send_start(image_size, image_crc, args.version)
        chunk_max = start_info["chunk_max"]
        expected_size = start_info["expected_size"]
        inactive_base = start_info["inactive_logical_base"]

        if expected_size != image_size:
            raise RuntimeError(
                f"Target expected size mismatch: expected_size={expected_size}, local={image_size}"
            )

        if chunk_max == 0:
            raise RuntimeError("Target returned chunk_max=0")

        actual_chunk = min(args.chunk_size, chunk_max)
        print(f"Inactive base   : 0x{inactive_base:08X}")
        print(f"Chunk size      : {actual_chunk} bytes")

        start_time = time.time()
        offset = 0

        while offset < image_size:
            chunk = image[offset:offset + actual_chunk]
            next_offset = flasher.send_data_chunk(offset, chunk)

            if next_offset != offset + len(chunk):
                raise RuntimeError(
                    f"Target ACK mismatch: expected next_offset={offset + len(chunk)}, got {next_offset}"
                )

            offset = next_offset
            print_progress(offset, image_size, start_time)

        print()
        finish_info = flasher.send_finish(image_size, image_crc)

        if finish_info["ok"] != 1:
            raise RuntimeError(f"Target reported unsuccessful finish, detail=0x{finish_info['detail']:08X}")

        print(f"Finish accepted : detail=0x{finish_info['detail']:08X}")
        print("Flash completed. Target should reset into the new bank.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted, sending ABORT...")
        flasher.send_abort()
        return 130
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        try:
            flasher.send_abort()
        except Exception:
            pass
        return 1
    finally:
        flasher.close()


def main():
    parser = argparse.ArgumentParser(description="STM32F767 UDP dual-bank firmware flasher")
    parser.add_argument("--ip", required=True, help="Target IPv4 address")
    parser.add_argument("--port", required=True, type=int, help="Target UDP command port")
    parser.add_argument("--bin", required=True, help="Path to .bin firmware image")
    parser.add_argument("--version", type=int, default=1, help="Firmware version field sent in START")
    parser.add_argument("--chunk-size", type=int, default=1024, help="Requested data chunk size")
    parser.add_argument("--timeout", type=float, default=1.0, help="UDP response timeout in seconds")
    parser.add_argument("--retries", type=int, default=8, help="Retries per packet")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    rc = flash_image(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
# python3 udp_flasher.py --ip 192.168.1.50 --port 5000 --bin app.bin --version 42
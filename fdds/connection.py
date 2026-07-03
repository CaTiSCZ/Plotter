"""
Device connection classes for FDDS UDP communication.

Provides DeviceConnection (base), NodeConnection, and CCUConnection
wrapping the UDP command/data protocol.
"""

import socket
import struct
import math
from typing import Optional

from .crc import crc16_ccitt
from .protocol import (
    CMD, PACKET, STRUCT, SAVE_KEY, RESET_KEY,
    UDP_CMD_PORT, UDP_DATA_PORT,
    RECEIVER_TYPE_DATA, RECEIVER_TYPE_LOG,
    GATHERING_DEVICES, SAMPLES_PER_PACKET, LINE_SEGMENTS,
    CALIBRATION_INFO_DATA_SIZE,
    data_packet_size, result_packet_size,
    ds_result_packet_size, sample_result_packet_size,
    DS_RESULT_HEADER, DS_RESULT_HEADER_SIZE,
    ACK_PACKET_HEADER_SIZE, LOG_PACKET_HEADER_SIZE,
    ALG_SEC_CCU_DS,
    DIGITAL_CHANNEL_FLAG_CURRENT, DIGITAL_CHANNEL_FLAG_LATCHED,
)


class ChannelInfo:
    """Parsed channel calibration info from device ID packet."""
    def __init__(self, unit: str, offset: float, gain: float):
        self.unit = unit
        self.offset = offset
        self.gain = gain

    def __repr__(self):
        return f"ChannelInfo(unit={self.unit!r}, offset={self.offset}, gain={self.gain})"


class DeviceConnection:
    """Base UDP connection to an FDDS device (node or CCU).

    Usage::

        with DeviceConnection("192.168.137.101") as dev:
            dev.get_id()
            print(dev.channels)
    """

    def __init__(self, ip: str, cmd_port: int = UDP_CMD_PORT,
                 timeout: float = 2.0, local_port: int = 0, name: str = "",
                 transport=None):
        self.ip = ip
        self.cmd_port = cmd_port
        self.timeout = timeout
        self.local_port = local_port
        self.name = name or ip
        self.sock: Optional[socket.socket] = None
        self.transport = transport
        self.channels: Optional[int] = None
        self.channel_info: list[ChannelInfo] = []
        self._local_ip: Optional[str] = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def open(self):
        # A non-UDP transport (serial / broadcast) carries commands itself; no
        # per-target UDP command socket is created in that case.
        if self.transport is not None:
            self.transport.open()
            self._local_ip = self.transport.local_ip or self._local_ip
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Determine local interface routing to target
        tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tmp.connect((self.ip, self.cmd_port))
            self._local_ip = tmp.getsockname()[0]
        finally:
            tmp.close()
        self.sock.bind((self._local_ip, self.local_port))
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.transport is not None:
            self.transport.close()
            return
        if self.sock:
            self.sock.close()
            self.sock = None

    @property
    def local_ip(self) -> str:
        return self._local_ip or ""

    @property
    def local_addr(self) -> tuple[str, int]:
        if self.sock:
            return self.sock.getsockname()
        return ("", 0)

    # ------------------------------------------------------------------
    # Low-level send/receive
    # ------------------------------------------------------------------

    def send_cmd(self, cmd: CMD, data: bytes = b'',
                 expect_response: bool = True,
                 timeout: float = None) -> Optional[bytes]:
        """Send a command and optionally wait for response."""
        pkt = STRUCT.CMD.pack(cmd) + data
        if self.transport is not None:
            if not expect_response:
                self.transport.send(pkt)
                return None
            return self.transport.request(
                pkt, timeout if timeout is not None else self.timeout)
        self.sock.sendto(pkt, (self.ip, self.cmd_port))
        if not expect_response:
            return None
        old = self.sock.gettimeout()
        try:
            if timeout is not None:
                self.sock.settimeout(timeout)
            resp, addr = self.sock.recvfrom(4096)
            return resp
        except socket.timeout:
            return None
        finally:
            if timeout is not None:
                self.sock.settimeout(old)

    def send_cmd_with_ack(self, cmd: CMD, data: bytes = b'') -> Optional[tuple[int, bytes]]:
        """Send a command and parse the ACK response.

        Returns (state, extra_data) on success, None on failure.
        """
        resp = self.send_cmd(cmd, data)
        if not resp or len(resp) < STRUCT.ACK.size:
            return None
        pkt_type, state, ack_cmd = STRUCT.ACK.unpack(resp[:STRUCT.ACK.size])
        if pkt_type != PACKET.ACK or ack_cmd != cmd:
            return None
        return (state, resp[STRUCT.ACK.size:])

    # ------------------------------------------------------------------
    # Common commands
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Ping the device. Returns True if responds."""
        return self.send_cmd(CMD.PING) is not None

    def enable_broadcast_rx(self, enable: bool) -> Optional[bool]:
        """Enable/disable acceptance of broadcast commands (RAM-only).

        Returns the resulting state (bool) or ``None`` on failure.
        """
        result = self.send_cmd_with_ack(
            CMD.ENABLE_BROADCAST_RX, bytes([1 if enable else 0]))
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 1:
            return None
        return bool(extra[0])

    def get_broadcast_rx(self) -> Optional[dict]:
        """Query broadcast-command acceptance.

        Returns ``{'enabled': bool, 'auto_locked': bool}`` or ``None`` on failure.
        """
        result = self.send_cmd_with_ack(CMD.GET_BROADCAST_RX)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 1:
            return None
        return {
            'enabled': bool(extra[0]),
            'auto_locked': bool(extra[1]) if len(extra) >= 2 else False,
        }

    def ping_node_rtt(self, target_ip: str = None, node_index: int = None,
                      count: int = 10, interval_ms: int = 100,
                      silent: bool = False, target_silent: bool = False,
                      icmp: bool = False,
                      timeout: float = None) -> Optional[dict]:
        """Ask this device to measure on-device RTT to another device.

        Addressing is either by explicit ``target_ip`` (works from any device) or
        by 1-based ``node_index`` (resolved from the CCU's node IP table, so it
        only works when this connection is to a CCU). The device times each ping
        with its PTP hardware clock and replies once the whole run completes.

        When ``silent`` is False (default) the pinging device also logs the run
        (a start line and a completion summary) via its debug log; set it True to
        keep the pinging device's log quiet. When ``target_silent`` is False
        (default) the pinged target logs "Ping received" for each probe; set it
        True to keep the target's log quiet.

        When ``icmp`` is True the device probes with a classic ICMP echo instead
        of the UDP ping, so reachability of any host (e.g. a control PC that does
        not answer the UDP ping) can be measured; ``target_silent`` then has no
        effect (the remote OS answers in its own stack).

        Returns a dict ``{origin_ip, target_ip, sent, recv, min_ns, avg_ns,
        max_ns, samples}`` (samples is a list of per-ping ns, ``None`` for a lost
        ping), or ``None`` on no/invalid response.
        """
        if (target_ip is None) == (node_index is None):
            raise ValueError("specify exactly one of target_ip or node_index")

        if node_index is not None:
            addr_mode = 1
            target_u32 = 0
            node_idx = int(node_index)
        else:
            addr_mode = 0
            target_u32 = struct.unpack('<I', socket.inet_aton(target_ip))[0]
            node_idx = 0

        req = STRUCT.PING_RTT_REQUEST.pack(addr_mode, node_idx, target_u32,
                                           int(count), int(interval_ms),
                                           1 if silent else 0,
                                           1 if target_silent else 0)

        # The firmware run can take up to count*(reply_timeout + interval); wait
        # long enough that the socket does not give up before the result ACK.
        if timeout is None:
            timeout = count * (1.0 + interval_ms / 1000.0) + 2.0

        cmd = CMD.PING_NODE_ICMP if icmp else CMD.PING_NODE

        resp = self.send_cmd(cmd, req, timeout=timeout)

        if not resp or len(resp) < ACK_PACKET_HEADER_SIZE + STRUCT.PING_RTT_RESULT.size:
            return None
        pkt_type, _state, ack_cmd = STRUCT.ACK.unpack(resp[:STRUCT.ACK.size])
        if pkt_type != PACKET.ACK or ack_cmd != cmd:
            return None

        extra = resp[ACK_PACKET_HEADER_SIZE:]
        (origin_u32, tgt_u32, sent, recv,
         min_ns, avg_ns, max_ns) = STRUCT.PING_RTT_RESULT.unpack(
            extra[:STRUCT.PING_RTT_RESULT.size])

        samples = []
        off = STRUCT.PING_RTT_RESULT.size
        for _ in range(sent):
            if off + STRUCT.PING_RTT_SAMPLE.size > len(extra):
                break
            val = STRUCT.PING_RTT_SAMPLE.unpack_from(extra, off)[0]
            samples.append(None if val == 0xFFFFFFFF else val)
            off += STRUCT.PING_RTT_SAMPLE.size

        return {
            'origin_ip': socket.inet_ntoa(struct.pack('<I', origin_u32)),
            'target_ip': socket.inet_ntoa(struct.pack('<I', tgt_u32)),
            'sent': sent,
            'recv': recv,
            'min_ns': min_ns,
            'avg_ns': avg_ns,
            'max_ns': max_ns,
            'samples': samples,
        }

    def get_system_state(self) -> Optional[dict]:
        """Query system/acquisition state. Returns dict or None on failure."""
        result = self.send_cmd_with_ack(CMD.GET_ACQUISITION_STATE)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 12:
            return None
        state_flags, ptp_locked, reserved = struct.unpack_from('<HBB', extra, 0)
        freq_hz, packets = struct.unpack_from('<II', extra, 4)
        result = {
            'state_flags': state_flags,
            'ptp_sync_locked': bool(ptp_locked),
            'tim12_freq_hz': freq_hz,
            'packets': packets,
        }
        if len(extra) >= 20:
            filt_time_err_ns = struct.unpack_from('<i', extra, 12)[0]
            delay_req_seq, last_resp_seq = struct.unpack_from('<HH', extra, 16)
            result['filt_time_err_ns'] = filt_time_err_ns
            result['delay_req_seq'] = delay_req_seq
            result['last_resp_seq'] = last_resp_seq
        return result

    def get_id(self) -> bool:
        """Query device ID and populate channels/channel_info."""
        resp = self.send_cmd(CMD.GET_ID)
        if not resp or len(resp) < STRUCT.ID.size:
            return False
        id_info = STRUCT.ID.unpack(resp[:STRUCT.ID.size])
        if id_info[0] != PACKET.ID:
            return False
        # CRC check
        crc = crc16_ccitt(resp[:-STRUCT.CRC.size])
        recv_crc = STRUCT.CRC.unpack(resp[-STRUCT.CRC.size:])[0]
        if crc != recv_crc:
            return False
        self.channels = id_info[-2]  # channels_count is second-to-last (last is _reserved)
        self.channel_info = []
        # The id_packet only physically carries ACQUISITION_CHANNELS channel_info
        # entries (node ADC channels). Devices such as the CCU report a larger
        # channels_count (one curve per fault output) but still send the fixed-size
        # packet, so only read the channel_info entries that actually fit.
        available = (len(resp) - STRUCT.ID.size - STRUCT.CRC.size) // STRUCT.CHANNEL.size
        readable = min(self.channels, max(available, 0))
        for i in range(readable):
            offset = STRUCT.ID.size + STRUCT.CHANNEL.size * i
            chunk = resp[offset:offset + STRUCT.CHANNEL.size]
            if len(chunk) < STRUCT.CHANNEL.size:
                return False
            unit_bytes, ch_offset, ch_gain = STRUCT.CHANNEL.unpack(chunk)
            unit = unit_bytes.decode('utf-8', errors='replace').rstrip('\x00')
            self.channel_info.append(ChannelInfo(unit, ch_offset, ch_gain))
        return True

    def get_fw_info(self) -> Optional[dict]:
        """Query CMD_GET_FW_ID and return parsed fw_info_t fields."""
        result = self.send_cmd_with_ack(CMD.GET_FW_ID)
        if result is None:
            return None
        state, extra = result
        if len(extra) < STRUCT.FW_INFO.size:
            return None
        fields = STRUCT.FW_INFO.unpack(extra[:STRUCT.FW_INFO.size])
        (fw_ver_id, fw_ver_major, fw_ver_minor,
         build_number, build_cfg_raw, build_time_raw, built_by_raw,
         variant_id, boot_bank, reserved,
         uptime_ms, reset_reason) = fields
        build_cfg = build_cfg_raw.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        build_time = build_time_raw.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        built_by = built_by_raw.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        return {
            "fw_version": {"id": fw_ver_id, "major": fw_ver_major, "minor": fw_ver_minor},
            "build_number": build_number,
            "build_cfg": build_cfg,
            "build_time": build_time,
            "built_by": built_by,
            "variant_id": variant_id,
            "boot_bank": boot_bank,
            "uptime_ms": uptime_ms,
            "reset_reason": reset_reason,
        }

    def get_digital_channels(self) -> Optional[list]:
        """Query CMD_GET_DIGITAL_CHANNELS — descriptors for the fault-output channels.

        Returns a list of dicts {label, flags, has_current, has_latched}, or None
        if the device did not respond (e.g. older firmware without this command).
        """
        result = self.send_cmd_with_ack(CMD.GET_DIGITAL_CHANNELS)
        if result is None:
            return None
        state, extra = result
        if len(extra) < STRUCT.DIGITAL_CHANNELS_HEADER.size:
            return None
        count = STRUCT.DIGITAL_CHANNELS_HEADER.unpack(
            extra[:STRUCT.DIGITAL_CHANNELS_HEADER.size])[0]
        channels = []
        base = STRUCT.DIGITAL_CHANNELS_HEADER.size
        for i in range(count):
            offset = base + STRUCT.DIGITAL_CHANNEL.size * i
            chunk = extra[offset:offset + STRUCT.DIGITAL_CHANNEL.size]
            if len(chunk) < STRUCT.DIGITAL_CHANNEL.size:
                break
            label_bytes, flags, _reserved = STRUCT.DIGITAL_CHANNEL.unpack(chunk)
            label = label_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
            channels.append({
                "label": label,
                "flags": flags,
                "has_current": bool(flags & DIGITAL_CHANNEL_FLAG_CURRENT),
                "has_latched": bool(flags & DIGITAL_CHANNEL_FLAG_LATCHED),
            })
        return channels

    def get_net_config(self) -> Optional[dict]:
        """Query CMD_GET_NET_CONFIG and return parsed net_info_t fields."""
        result = self.send_cmd_with_ack(CMD.GET_NET_CONFIG)
        if result is None:
            return None
        state, extra = result
        if len(extra) < STRUCT.NET_INFO.size:
            return None
        mac_raw, _pad, ip_addr, netmask, gateway = STRUCT.NET_INFO.unpack(extra[:STRUCT.NET_INFO.size])
        mac = ':'.join(f'{b:02X}' for b in mac_raw)
        ip = socket.inet_ntoa(ip_addr.to_bytes(4, 'little'))
        mask = socket.inet_ntoa(netmask.to_bytes(4, 'little'))
        gw = socket.inet_ntoa(gateway.to_bytes(4, 'little'))
        return {
            "mac": mac,
            "ip": ip,
            "netmask": mask,
            "gateway": gw,
        }

    def get_loop_stats(self, reset: bool = True) -> Optional[dict]:
        """Query CMD_GET_LOOP_STATS — main-loop timing watchdog statistics.

        Returns a dict with ``max_loop_us`` (worst observed main-loop iteration
        duration in microseconds) and ``overrun_count`` (number of iterations that
        exceeded the 1 ms budget). The firmware always resets its accumulators after
        replying; the ``reset`` argument is accepted for API symmetry.
        """
        result = self.send_cmd_with_ack(CMD.GET_LOOP_STATS)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 8:
            return None
        max_loop_us, overrun_count = struct.unpack_from('<II', extra, 0)
        return {
            "max_loop_us": max_loop_us,
            "overrun_count": overrun_count,
        }

    def register_receiver(self, receiver_ip: str, receiver_port: int,
                          receiver_type: int = RECEIVER_TYPE_DATA,
                          save: bool = False) -> bool:
        """Register a receiver on the device."""
        ip_bytes = socket.inet_aton(receiver_ip)
        payload = ip_bytes + struct.pack('<H', receiver_port)
        payload += struct.pack('B', receiver_type)
        if save:
            payload += struct.pack('B', SAVE_KEY)
        result = self.send_cmd_with_ack(CMD.REGISTER_RECEIVER, payload)
        return result is not None

    def remove_receiver(self, receiver_ip: str, receiver_port: int,
                        receiver_type: int = RECEIVER_TYPE_DATA,
                        save: bool = False) -> Optional[bool]:
        """Remove a receiver from the device.

        Returns True if removed, False if not found, None on comm failure.
        When ``save`` is set, the persisted default receiver of this type is
        also erased from EEPROM so it is not auto-registered on the next boot.
        """
        ip_bytes = socket.inet_aton(receiver_ip)
        payload = ip_bytes + struct.pack('<H', receiver_port)
        payload += struct.pack('B', receiver_type)
        if save:
            payload += struct.pack('B', SAVE_KEY)
        result = self.send_cmd_with_ack(CMD.REMOVE_RECEIVER, payload)
        if result is None:
            return None
        state, extra = result
        # If response data is all 0xFF, receiver was not found
        if len(extra) >= 6 and all(b == 0xFF for b in extra[:6]):
            return False
        return True

    def list_receivers(self, receiver_type: int = RECEIVER_TYPE_DATA) -> Optional[list[tuple[str, int]]]:
        """List registered (RAM) receivers of given type.

        Returns list of (ip, port) tuples, or None on comm failure.
        """
        result = self.list_receivers_full(receiver_type)
        if result is None:
            return None
        receivers, _eeprom_default = result
        return receivers

    def list_receivers_full(
        self, receiver_type: int = RECEIVER_TYPE_DATA
    ) -> Optional[tuple[list[tuple[str, int]], Optional[tuple[str, int]]]]:
        """List registered receivers and the EEPROM-persisted default of given type.

        Response layout (firmware):
            [0]      ram_count
            [1]      eeprom_valid (0/1)
            [2..7]   eeprom default ip(4)+port(2)
            [8..]    ram_count entries, ip(4)+port(2) each

        Returns a tuple ``(receivers, eeprom_default)`` where ``receivers`` is a
        list of (ip, port) tuples and ``eeprom_default`` is (ip, port) or None.
        Returns None on comm failure.
        """
        payload = b'\x00' * 6 + struct.pack('B', receiver_type)
        result = self.send_cmd_with_ack(CMD.GET_RECEIVERS, payload)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 8:
            # Unexpected / legacy short response: no header available.
            return [], None
        ram_count = extra[0]
        eeprom_valid = extra[1]
        eeprom_default: Optional[tuple[str, int]] = None
        if eeprom_valid:
            ip = socket.inet_ntoa(extra[2:6])
            port = struct.unpack('<H', extra[6:8])[0]
            eeprom_default = (ip, port)
        receivers = []
        base = 8
        for i in range(ram_count):
            entry = extra[base + i * 6: base + i * 6 + 6]
            if len(entry) < 6:
                break
            ip = socket.inet_ntoa(entry[0:4])
            port = struct.unpack('<H', entry[4:6])[0]
            receivers.append((ip, port))
        return receivers, eeprom_default

    def set_calibration(self, channels: int,
                        info: list[tuple[str, float, float]],
                        save: bool = True,
                        cal_time: int = 0, cal_id: int = 0) -> bool:
        """Set channel calibration on device.

        Args:
            channels: number of channels
            info: list of (unit_str, offset, gain) per channel
            save: persist to EEPROM
            cal_time: calibration timestamp (uint64, Unix epoch seconds)
            cal_id: calibration identifier (uint32)
        """
        pkt = struct.pack('<IBxH', int(CMD.SET_CALIBRATION),
                          SAVE_KEY if save else 0, channels)
        for unit, offset, gain in info:
            pkt += struct.pack('<3sxff', unit.encode('utf-8')[:3], offset, gain)
        # Append calibration_info_t (12 bytes)
        pkt += struct.pack('<QI', cal_time, cal_id)
        crc_val = crc16_ccitt(pkt)
        pkt += struct.pack('<H', crc_val)
        self.sock.sendto(pkt, (self.ip, self.cmd_port))
        try:
            resp, addr = self.sock.recvfrom(4096)
            return resp is not None and len(resp) > 0
        except socket.timeout:
            return False

    def get_calibration(self, source: int = 0) -> Optional[dict]:
        """Query CMD_GET_CALIBRATION (node only).

        Args:
            source: 0=RAM, 1=EEPROM
        """
        payload = struct.pack('B', source)
        result = self.send_cmd_with_ack(CMD.GET_CALIBRATION, payload)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 3:
            return None
        src = extra[0]
        channels_count = struct.unpack('<H', extra[1:3])[0]
        offset = 3
        channels = []
        for i in range(channels_count):
            end = offset + STRUCT.CHANNEL.size
            if end > len(extra):
                break
            unit_bytes, ch_offset, ch_gain = STRUCT.CHANNEL.unpack(extra[offset:end])
            unit = unit_bytes.decode('utf-8', errors='replace').rstrip('\x00')
            channels.append({"unit": unit, "offset": ch_offset, "gain": ch_gain})
            offset = end
        cal_info = None
        if offset + CALIBRATION_INFO_DATA_SIZE <= len(extra):
            cal_time, cal_id = STRUCT.CALIBRATION_INFO.unpack(
                extra[offset:offset + CALIBRATION_INFO_DATA_SIZE])
            cal_info = {"time": cal_time, "id": cal_id}
        return {
            "source": src,
            "channels": channels,
            "calibration_info": cal_info,
        }

    def get_channel_minmax(self, source: int = 0) -> Optional[dict]:
        """Query CMD_GET_CHANNEL_MINMAX (node only).

        Args:
            source: 0=RAM runtime stats, 1=EEPROM persisted stats

        Returns:
            dict with source and per-channel min/max snapshots, or None on failure.
        """
        payload = struct.pack('B', source)
        result = self.send_cmd_with_ack(CMD.GET_CHANNEL_MINMAX, payload)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 3:
            return None

        src = extra[0]
        channels_count = struct.unpack('<H', extra[1:3])[0]
        offset = 3
        channels = []
        for _ in range(channels_count):
            end = offset + 8
            if end > len(extra):
                break
            absolute_min, absolute_max, run_min, run_max = struct.unpack('<hhhh', extra[offset:end])
            channels.append({
                "absolute_min": absolute_min,
                "absolute_max": absolute_max,
                "run_min": run_min,
                "run_max": run_max,
            })
            offset = end

        return {
            "source": src,
            "channels": channels,
        }

    def reset_channel_minmax(self, storage_mask: int = 0x01, groups_mask: int = 0x03) -> Optional[dict]:
        """Reset CMD_RESET_CHANNEL_MINMAX (node only).

        Args:
            storage_mask: bit0=RAM, bit1=EEPROM
            groups_mask: bit0=run, bit1=absolute
        """
        payload = struct.pack('BB', storage_mask & 0x03, groups_mask & 0x03)
        result = self.send_cmd_with_ack(CMD.RESET_CHANNEL_MINMAX, payload)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 3:
            return None

        st_mask = extra[0]
        gp_mask = extra[1]
        status = extra[2]
        return {
            "storage_mask": st_mask,
            "groups_mask": gp_mask,
            "ram_ok": bool(status & 0x01),
            "eeprom_ok": bool(status & 0x02),
            "guard_applied": bool(status & 0x04),
            "status": status,
        }

    def set_clock_config(self, config_byte: int, save: bool = False) -> bool:
        """Set clock configuration (source + output enable)."""
        payload = struct.pack('B', config_byte)
        if save:
            payload += struct.pack('B', SAVE_KEY)
        result = self.send_cmd_with_ack(CMD.CLOCK_CONFIG, payload)
        return result is not None

    def reset_counters(self) -> bool:
        """Reset sampling/gathering packet counters."""
        result = self.send_cmd_with_ack(CMD.RESET_COUNTERS)
        return result is not None

    def reset_device(self) -> bool:
        """Reset the device."""
        payload = struct.pack('B', RESET_KEY)
        self.send_cmd(CMD.RESET_DEVICE, payload, expect_response=False)
        return True

    def get_ptp_time(self) -> Optional[tuple[int, int]]:
        """Read PTP time from the device.

        Returns (seconds, nanoseconds) or None on failure.
        """
        result = self.send_cmd_with_ack(CMD.GET_PTP_TIME)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 8:
            return None
        sec, ns = struct.unpack('<II', extra[:8])
        return (sec, ns)

    def get_ds_state(self) -> Optional[dict]:
        """Read digital shadow internal state from CCU.

        Returns:
            dict with frame_count, phase, z_line[3],
            identification_length, or None on failure.
        """
        result = self.send_cmd_with_ack(CMD.GET_DS_STATE)
        if result is None:
            return None
        state, extra = result
        if len(extra) < STRUCT.DS_STATE.size:
            return None
        (frame_count, phase, _reserved, z1, z2, z3, identification_length, _reserved2) = STRUCT.DS_STATE.unpack(
            extra[:STRUCT.DS_STATE.size])
        return {
            "frame_count": frame_count,
            "phase": phase,
            "z_line": [z1, z2, z3],
            "identification_length": identification_length,
        }

    def get_ccu_physical_chunk(self, start_idx: int, count: int) -> Optional[dict]:
        """Read a chunk of the latest CCU physical-unit sample window.

        Args:
            start_idx: sample index in [0, SAMPLES_PER_PACKET)
            count: number of samples to fetch (FW caps the maximum)

        Returns:
            dict with packet_num, start_idx, count, samples[list[list[float]]], or None on failure.
        """
        payload = struct.pack('<HH', int(start_idx), int(count))
        for _ in range(3):
            result = self.send_cmd_with_ack(CMD.GET_CCU_PHYSICAL, payload)
            if result is None:
                continue

            _state, extra = result
            if len(extra) < 6:
                continue

            packet_num, start_out, count_out = struct.unpack('<HHH', extra[:6])
            values_count = int(count_out) * 8
            expected_len = 6 + values_count * 4
            if len(extra) < expected_len:
                continue

            values = struct.unpack('<' + ('f' * values_count), extra[6:expected_len])
            samples = [list(values[i * 8:(i + 1) * 8]) for i in range(int(count_out))]
            return {
                "packet_num": int(packet_num),
                "start_idx": int(start_out),
                "count": int(count_out),
                "samples": samples,
            }
        return None

    def set_busywait(self, delay_us: int) -> Optional[int]:
        """Inject a busy-wait delay on the device (test mode only).

        Args:
            delay_us: Delay in microseconds (capped at 100000 by FW).

        Returns:
            The actual delay applied (echoed by the device), or None on failure.
        """
        payload = struct.pack('<I', delay_us)
        result = self.send_cmd_with_ack(CMD.BUSYWAIT, payload)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 4:
            return None
        return struct.unpack('<I', extra[:4])[0]

    def set_hw_info(self, target: int, version_id: int,
                    major: int, minor: int, serial: int) -> bool:
        """Set HW version and serial number in device EEPROM.

        Args:
            target: 0 = MCU (digital board), 1 = ADC (analog board)
            version_id: hardware version ID (u16)
            major: major version (u8)
            minor: minor version (u8)
            serial: serial number (u32)
        """
        payload = struct.pack('<BB2xHBBI', target, SAVE_KEY,
                              version_id, major, minor, serial)
        result = self.send_cmd_with_ack(CMD.HW_CONFIG, payload)
        if result is None:
            return False
        state, _ = result
        return state == 0

    def startup_control(self, sub_cmd: int) -> Optional[tuple[int, bytes]]:
        """Send CMD_STARTUP_CONTROL with a sub-command byte."""
        payload = struct.pack('B', sub_cmd)
        return self.send_cmd_with_ack(CMD.STARTUP_CONTROL, payload)

    def startup_start(self, samples: int = 0) -> bool:
        """Start the autonomous startup sequence. samples=0 means infinite."""
        payload = struct.pack('<BI', 1, samples)
        return self.send_cmd_with_ack(CMD.STARTUP_CONTROL, payload) is not None

    def startup_abort(self) -> bool:
        """Abort an in-progress startup."""
        return self.startup_control(0) is not None

    def stop_system(self) -> bool:
        """Stop the system (gathering + measurement)."""
        return self.send_cmd_with_ack(CMD.STOP_SYSTEM) is not None

    def get_auto_start(self) -> Optional[bool]:
        """Query auto-start setting. Returns True/False or None on failure."""
        result = self.startup_control(2)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 1:
            return None
        return extra[0] != 0

    def set_auto_start(self, enable: bool) -> bool:
        """Enable (3) or disable (4) auto-start in EEPROM."""
        return self.startup_control(3 if enable else 4) is not None

    def set_node_ips(self, ips: list[str]) -> bool:
        """Write expected node IPv4 addresses to CCU EEPROM (CMD_SET_NODE_IPS).

        Each address is sent as 4 network-order bytes (inet_aton). Unused slots
        (fewer than 4 addresses, or an explicit None) are stored as 0xFFFFFFFF.
        """
        padded: list[str | None] = list(ips[:4])
        while len(padded) < 4:
            padded.append(None)
        payload = b''
        for ip in padded:
            if ip is None:
                payload += struct.pack('<I', 0xFFFFFFFF)
            else:
                payload += socket.inet_aton(ip)
        return self.send_cmd_with_ack(CMD.SET_NODE_IPS, payload) is not None

    def get_node_ips(self) -> list[str | None] | None:
        """Read expected node IPv4 addresses from CCU EEPROM (CMD_GET_NODE_IPS).

        Returns a list of 4 entries (one per slot); unused slots (0xFFFFFFFF or
        0.0.0.0) are returned as None. Returns None on communication failure.
        """
        result = self.send_cmd_with_ack(CMD.GET_NODE_IPS)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 16:
            return None
        out: list[str | None] = []
        for i in range(4):
            raw = extra[i * 4:i * 4 + 4]
            if raw in (b'\xff\xff\xff\xff', b'\x00\x00\x00\x00'):
                out.append(None)
            else:
                out.append(socket.inet_ntoa(raw))
        return out

    def set_net_config(self, ip: str, netmask: str = "255.255.255.0",
                       gateway: str | None = None) -> bool:
        """Persist a new IPv4 configuration to EEPROM (CMD_SET_NET_CONFIG).

        The change takes effect after the device is reset. If ``gateway`` is
        omitted it defaults to ``.1`` of the new subnet. Requires SAVE_KEY.
        """
        if gateway is None:
            gateway = ip.rsplit('.', 1)[0] + '.1'
        payload = struct.pack('B', SAVE_KEY)
        payload += socket.inet_aton(ip)
        payload += socket.inet_aton(netmask)
        payload += socket.inet_aton(gateway)
        return self.send_cmd_with_ack(CMD.SET_NET_CONFIG, payload) is not None

    def set_trigger_config(self, config_byte: int, holdoff_ns: int = 0,
                           save: bool = False) -> Optional[tuple[int, int]]:
        """Set trigger configuration (config byte + hold-off).

        Args:
            config_byte: Encoded trigger config byte (bit0=enable, bits2:1=edge, bits4:3=pull).
            holdoff_ns: Hold-off time in nanoseconds (0 = no hold-off).
            save: If True, persist to EEPROM.

        Returns:
            (current_config_byte, current_holdoff_ns) or None on failure.
        """
        payload = struct.pack('<BQ', config_byte, holdoff_ns)
        if save:
            payload += struct.pack('B', SAVE_KEY)
        result = self.send_cmd_with_ack(CMD.SET_TRIGGER_CONFIG, payload)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 9:
            return None
        cfg = extra[0]
        ho = struct.unpack('<Q', extra[1:9])[0]
        return (cfg, ho)

    def get_trigger_config(self) -> Optional[tuple[int, int]]:
        """Read current trigger configuration.

        Returns:
            (config_byte, holdoff_ns) or None on failure.
        """
        result = self.send_cmd_with_ack(CMD.GET_TRIGGER_CONFIG)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 9:
            return None
        cfg = extra[0]
        ho = struct.unpack('<Q', extra[1:9])[0]
        return (cfg, ho)

    def gpio_diag(self, port: int, pin: int,
                  value: Optional[int] = None) -> Optional[int]:
        """Diagnostic: read a pin's level, or drive it as a push-pull output.

        Convenience wrapper around the richer CMD_GPIO_DIAG protocol. Only
        DGPIO/AGPIO/TEST/EXT_TRIGGER pins are addressable (firmware whitelist).

        Args:
            port: GPIO port index (0=A, 1=B, ... 10=K).
            pin: Pin number (0..15).
            value: If None, read the pin. Otherwise configure it as a
                   push-pull output driven to 0 or 1. A pull-down is enabled so
                   that the pad keeps a defined (low) level even when the bench
                   loopback wire is absent — the active push-pull drive is
                   unaffected by the pull.

        Returns:
            The pin's input-data-register level (0 or 1) after the operation,
            or None on failure / invalid / not-allowed pin.
        """
        if value is None:
            info = self.gpio_read(port, pin)
        else:
            info = self.gpio_configure(port, pin, mode=1, pull=2, otype=0,
                                       af=0, level=1 if value else 0)
        if info is None:
            return None
        return info['level']

    def gpio_read(self, port: int, pin: int) -> Optional[dict]:
        """Read all live settings of a DGPIO/AGPIO/TEST pin via CMD_GPIO_DIAG.

        Returns a dict with keys: mode (0=input,1=output,2=alternate,3=analog),
        pull (0=none,1=up,2=down), otype (0=push-pull,1=open-drain),
        af (0..15), speed (0..3), level (live input level), odr (output latch),
        or None on failure / invalid / not-allowed pin.
        """
        payload = struct.pack('<BBB', 0, port, pin)
        return self._gpio_diag_request(payload)

    def gpio_configure(self, port: int, pin: int, mode: int,
                       pull: int = 0, otype: int = 0, af: int = 0,
                       level: int = 0) -> Optional[dict]:
        """Configure a DGPIO/AGPIO/TEST pin via CMD_GPIO_DIAG.

        Args:
            port: GPIO port index (0=A..10=K).
            pin: Pin number (0..15).
            mode: 0=input, 1=output, 2=alternate, 3=analog.
            pull: 0=none, 1=up, 2=down.
            otype: 0=push-pull, 1=open-drain (output/alternate only).
            af: Alternate-function number 0..15 (mode=alternate).
            level: Initial output value 0/1 (mode=output).

        Returns the post-operation settings dict (see gpio_read), or None on
        failure / invalid / not-allowed pin.
        """
        payload = struct.pack('<BBBBBBBB', 1, port, pin,
                              mode & 0xFF, pull & 0xFF, otype & 0xFF,
                              af & 0xFF, 1 if level else 0)
        return self._gpio_diag_request(payload)

    def gpio_default(self, port: int, pin: int) -> Optional[dict]:
        """Restore a pin to its post-firmware-init configuration via CMD_GPIO_DIAG.

        For EXT_TRIGGER_INPUT (PE0) this re-applies the live trigger/clock pin
        configuration; for every other whitelisted pin it restores the snapshot
        captured at the end of startup.

        Returns the post-operation settings dict (see gpio_read), or None on
        failure / invalid / not-allowed pin.
        """
        payload = struct.pack('<BBB', 2, port, pin)
        return self._gpio_diag_request(payload)

    def _gpio_diag_request(self, payload: bytes) -> Optional[dict]:
        """Send a CMD_GPIO_DIAG request and parse the 10-byte ACK payload."""
        result = self.send_cmd_with_ack(CMD.GPIO_DIAG, payload)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 10 or extra[0] != 1:
            return None
        return {
            'port': extra[1],
            'pin': extra[2],
            'mode': extra[3],
            'pull': extra[4],
            'otype': extra[5],
            'af': extra[6],
            'speed': extra[7],
            'level': extra[8],
            'odr': extra[9],
        }

    def force_trigger(self, ptp_sec: Optional[int] = None,
                      ptp_ns: Optional[int] = None) -> bool:
        """Force a trigger event.

        If ptp_sec/ptp_ns are provided, schedules the trigger at that PTP time.
        Otherwise triggers immediately.
        """
        if ptp_sec is not None and ptp_ns is not None:
            payload = struct.pack('<II', ptp_sec, ptp_ns)
        else:
            payload = b''
        self.send_cmd(CMD.FORCE_TRIGGER, payload, expect_response=False)
        return True

    def resend_packet(self, packet_num: int) -> Optional[int]:
        """Request retransmission of a previously sent packet by sequence number.

        The firmware sends the retransmitted DATA packet (818B) to our CMD socket
        before the ACK, so we must skip non-ACK packets in the receive loop.

        Returns:
            0 on success (packet retransmitted), 0xFF if not found, None on comm failure.
        """
        payload = struct.pack('<H', packet_num)
        pkt = STRUCT.CMD.pack(CMD.RESEND_PACKET) + payload
        self.sock.sendto(pkt, (self.ip, self.cmd_port))
        # Loop to skip the retransmitted DATA packet that arrives before the ACK
        for _ in range(3):
            try:
                resp, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                return None
            if len(resp) < STRUCT.ACK.size:
                continue
            pkt_type, state, ack_cmd = STRUCT.ACK.unpack(resp[:STRUCT.ACK.size])
            if pkt_type == PACKET.ACK and ack_cmd == CMD.RESEND_PACKET:
                extra = resp[STRUCT.ACK.size:]
                if len(extra) < 1:
                    return None
                return extra[0]
        return None

    def set_ds_impedance(self, z1: float, z2: float, z3: float,
                         save: bool = False) -> Optional[bool]:
        """Inject digital-shadow impedance values into the running algorithm.

        Implemented via the unified algorithm-config store: writes the CCU
        digital-shadow section (ALG_SEC_CCU_DS), preserving the current
        identification length.

        Args:
            z1, z2, z3: Line impedances in ohms.
            save: If True, also persist the full algorithm config to flash.

        Returns:
            True if applied (and saved when requested), False on failure,
            None on communication failure.
        """
        # Preserve the currently configured identification length.
        id_length = 1000
        sec = self.get_alg_config_section(ALG_SEC_CCU_DS)
        if sec is not None and len(sec['body']) >= 14:
            id_length = struct.unpack_from('<H', sec['body'], 12)[0]
        data = struct.pack('<fffH', z1, z2, z3, id_length)
        res = self.set_alg_config_section(ALG_SEC_CCU_DS, data, save=save)
        if res is None:
            return None
        return res['saved'] if save else res['applied']

    def save_ds_impedance(self) -> Optional[bool]:
        """Persist the current algorithm config (incl. live digital-shadow model).

        Snapshots the live impedance model: if identification is complete
        (phase 2) the identified impedance is stored, otherwise a NaN sentinel is
        stored so the model is re-identified after the next boot.

        Returns:
            True on success, False on wrong key / flash error,
            None on communication failure.
        """
        return self.save_alg_config()

    def force_ds_identification(self) -> bool:
        """Force digital-shadow algorithm back to the identification phase.

        Returns:
            True on success, False on communication failure.
        """
        result = self.send_cmd_with_ack(CMD.FORCE_DS_IDENTIFICATION)
        return result is not None

    def get_ds_saved_impedance(self) -> Optional[dict]:
        """Read digital-shadow impedance saved in the algorithm-config flash.

        Returns:
            dict with keys 'z1', 'z2', 'z3' (floats, ohms), 'valid' (bool) and
            'id_length' (int), or None on communication failure. 'valid' is True
            only when a record exists AND the stored impedance is finite.
        """
        s = self.get_alg_config_saved_section(ALG_SEC_CCU_DS)
        if s is None:
            return None
        if not s['valid'] or len(s['body']) < 14:
            return {'z1': float('nan'), 'z2': float('nan'), 'z3': float('nan'),
                    'valid': False, 'id_length': 0}
        z1, z2, z3, id_length = struct.unpack_from('<fffH', s['body'], 0)
        finite = not (math.isnan(z1) or math.isnan(z2) or math.isnan(z3))
        return {'z1': z1, 'z2': z2, 'z3': z3, 'valid': finite, 'id_length': id_length}

    # ------------------------------------------------------------------
    # Generic algorithm-config access (CMD_*_ALG_CONFIG)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_alg_get(extra: bytes) -> Optional[dict]:
        """Parse a CMD_GET_ALG_CONFIG ACK: tag(u16) schema(u16) section(u8) body."""
        if len(extra) < 5:
            return None
        tag, schema, section = struct.unpack_from('<HHB', extra, 0)
        return {'tag': tag, 'schema': schema, 'section': section, 'body': extra[5:]}

    @staticmethod
    def _parse_alg_get_saved(extra: bytes) -> Optional[dict]:
        """Parse a CMD_GET_ALG_CONFIG_SAVED ACK: tag schema section valid body."""
        if len(extra) < 6:
            return None
        tag, schema, section, valid = struct.unpack_from('<HHBB', extra, 0)
        return {'tag': tag, 'schema': schema, 'section': section,
                'valid': bool(valid), 'body': extra[6:]}

    def get_alg_config_section(self, section_id: int) -> Optional[dict]:
        """Read a section of the live (RAM) algorithm parameters.

        Args:
            section_id: an ALG_SEC_* id, or ALG_SEC_ALL (0xFF) for the whole blob.

        Returns:
            dict with 'tag', 'schema', 'section', 'body' (bytes), or None on
            communication failure / unknown section.
        """
        result = self.send_cmd_with_ack(CMD.GET_ALG_CONFIG, bytes([section_id & 0xFF]))
        if result is None:
            return None
        _state, extra = result
        parsed = self._parse_alg_get(extra)
        # A 1-byte NACK (len < 5) indicates an unknown section.
        return parsed

    def get_alg_config_saved_section(self, section_id: int) -> Optional[dict]:
        """Read a section of the persisted algorithm parameters from flash."""
        result = self.send_cmd_with_ack(CMD.GET_ALG_CONFIG_SAVED, bytes([section_id & 0xFF]))
        if result is None:
            return None
        _state, extra = result
        return self._parse_alg_get_saved(extra)

    def set_alg_config_section(self, section_id: int, data: bytes,
                               save: bool = False) -> Optional[dict]:
        """Update one section of the live algorithm parameters.

        Args:
            section_id: an ALG_SEC_* id.
            data: section payload bytes (length must match the section).
            save: if True, append SAVE_KEY to also persist all sections to flash.

        Returns:
            dict {'applied': bool, 'saved': bool}, or None on communication failure.
        """
        payload = bytes([section_id & 0xFF]) + bytes(data)
        if save:
            payload += bytes([SAVE_KEY])
        result = self.send_cmd_with_ack(CMD.SET_ALG_CONFIG, payload)
        if result is None:
            return None
        _state, extra = result
        applied = bool(extra[0]) if len(extra) >= 1 else False
        saved = bool(extra[1]) if len(extra) >= 2 else False
        return {'applied': applied, 'saved': saved}

    def save_alg_config(self) -> Optional[bool]:
        """Persist all current (RAM) algorithm parameters to flash atomically.

        Returns:
            True on success, False on wrong key / flash error,
            None on communication failure.
        """
        result = self.send_cmd_with_ack(CMD.SAVE_ALG_CONFIG, bytes([SAVE_KEY]))
        if result is None:
            return None
        _state, extra = result
        return bool(extra[0]) if extra else False


class NodeConnection(DeviceConnection):
    """Connection to an FDDS node (measurement unit)."""

    def __init__(self, ip: str, node_index: int = 0, **kwargs):
        name = kwargs.pop('name', f"node{node_index}")
        super().__init__(ip, name=name, **kwargs)
        self.node_index = node_index
        self.data_port: Optional[int] = None
        self.test_mode_enabled = False

    def enable_test_mode(self) -> bool:
        """Enable test data input mode. Returns True on success."""
        result = self.send_cmd_with_ack(CMD.ENABLE_TEST_DATA_INPUT)
        if result is None:
            self.test_mode_enabled = False
            return False
        state, extra = result
        if len(extra) < STRUCT.PORT.size:
            self.test_mode_enabled = False
            return False
        port = STRUCT.PORT.unpack(extra[:STRUCT.PORT.size])[0]
        if port == 0:
            self.test_mode_enabled = False
            return False
        self.data_port = port
        self.test_mode_enabled = True
        return True

    def disable_test_mode(self) -> bool:
        """Disable test data input mode."""
        result = self.send_cmd_with_ack(CMD.DISABLE_TEST_DATA_INPUT)
        if result is not None:
            self.test_mode_enabled = False
            return True
        return False

    def start_sampling(self, samples: int = 0, wait_trigger: bool = False) -> bool:
        """Start acquisition. samples=0 means infinite."""
        cmd = CMD.START_ON_TRIGGER if wait_trigger else CMD.START_SAMPLING
        payload = struct.pack('<I', samples)
        result = self.send_cmd_with_ack(cmd, payload)
        return result is not None

    def start_sampling_deferred(self, sec: int, ns: int, samples: int = 0) -> Optional[int]:
        """Start acquisition with PTP-deferred trigger.

        Args:
            sec: PTP seconds at which to start
            ns: PTP nanoseconds at which to start
            samples: number of sample-chunks to acquire (0 = infinite)

        Returns:
            Response code (0=OK, 1=bad payload, 2=time too close), or None on failure.
        """
        payload = struct.pack('<III', sec, ns, samples)
        result = self.send_cmd_with_ack(CMD.START_SAMPLING_DEFERRED, payload)
        if result is None:
            return None
        state, extra = result
        if len(extra) >= 1:
            return extra[0]
        return 0

    def stop_sampling(self) -> Optional[int]:
        """Stop acquisition. Returns number of packets sampled, or None on failure."""
        result = self.send_cmd_with_ack(CMD.STOP_SAMPLING)
        if result is None:
            return None
        state, extra = result
        if len(extra) >= 4:
            return struct.unpack('<I', extra[:4])[0]
        return 0

    def send_data_packet(self, packet_bytes: bytes) -> None:
        """Send a pre-built data packet to the node's test data input port."""
        if self.data_port is None:
            raise RuntimeError(f"[{self.name}] Test mode not enabled, no data port")
        self.sock.sendto(packet_bytes, (self.ip, self.data_port))

    def test_crc_fault(self, mode: int, count: int = 0) -> Optional[tuple[int, int]]:
        """Arm/disarm deliberate data-packet CRC corruption (CMD_TEST_CRC_FAULT).

        Args:
            mode: 0=off, 1=recoverable (corrupt only fresh transmissions, finite
                  count), 2=unrecoverable (corrupt fresh + all resends until off).
            count: mode-1 number of fresh packets to corrupt (0 -> firmware default
                   of 1, 0xFFFF -> until disabled). Ignored for modes 0/2.

        Returns:
            (active_mode, remaining) echoed by the firmware, or None on failure.
        """
        payload = struct.pack('<BH', mode & 0xFF, count & 0xFFFF)
        result = self.send_cmd_with_ack(CMD.TEST_CRC_FAULT, payload)
        if result is None:
            return None
        _state, extra = result
        if len(extra) < 3:
            return None
        active_mode = extra[0]
        remaining = struct.unpack_from('<H', extra, 1)[0]
        return (active_mode, remaining)


class CCUConnection(DeviceConnection):
    """Connection to the FDDS CCU (Central Computation Unit)."""

    TEST_ROUTING_ROUND_ROBIN = "round-robin"
    TEST_ROUTING_IP = "ip"

    def __init__(self, ip: str, **kwargs):
        name = kwargs.pop('name', 'ccu')
        super().__init__(ip, name=name, **kwargs)
        self.data_port: int = UDP_DATA_PORT
        self.test_mode_enabled = False

    def enable_test_mode(self, ds_bypass: bool = False, routing_mode: str = TEST_ROUTING_ROUND_ROBIN) -> bool:
        """Enable CCU test mode.

        Args:
            ds_bypass: if True, enable DS bypass mode — CCU will skip
                       digital_shadow() and use deviation_U from incoming
                       PACKET_DS_RESULT packets instead.
            routing_mode: "round-robin" for direct packets from one IP,
                          "ip" to route packets by source IP while keeping
                          CCU test-mode outputs enabled.
        """
        routing_map = {
            self.TEST_ROUTING_ROUND_ROBIN: 0,
            self.TEST_ROUTING_IP: 1,
        }
        if routing_mode not in routing_map:
            raise ValueError(f"Unsupported CCU test routing mode: {routing_mode}")

        payload = struct.pack('BB', 0x01 if ds_bypass else 0x00, routing_map[routing_mode])
        result = self.send_cmd_with_ack(CMD.ENABLE_TEST_DATA_INPUT, payload)
        if result is None:
            self.test_mode_enabled = False
            return False
        state, extra = result
        if len(extra) >= STRUCT.PORT.size:
            port = STRUCT.PORT.unpack(extra[:STRUCT.PORT.size])[0]
            if port != 0:
                self.data_port = port
        self.test_mode_enabled = True
        return True

    def disable_test_mode(self) -> bool:
        """Disable CCU test mode."""
        result = self.send_cmd_with_ack(CMD.DISABLE_TEST_MODE)
        if result is not None:
            self.test_mode_enabled = False
            return True
        return False

    def start_gathering(self, samples: int = 0, wait_trigger: bool = False) -> bool:
        """Start gathering from nodes. samples=0 means infinite."""
        cmd = CMD.START_ON_TRIGGER if wait_trigger else CMD.START_SAMPLING
        payload = struct.pack('<I', samples)
        result = self.send_cmd_with_ack(cmd, payload)
        return result is not None

    def stop_gathering(self) -> Optional[int]:
        """Stop gathering. Returns chunks counter, or None on failure."""
        result = self.send_cmd_with_ack(CMD.STOP_SAMPLING)
        if result is None:
            return None
        state, extra = result
        if len(extra) >= 4:
            return struct.unpack('<I', extra[:4])[0]
        return 0

    def send_node_data_packet(self, packet_bytes: bytes) -> None:
        """Send a data packet to CCU's data port (for CCU-only test mode)."""
        self.sock.sendto(packet_bytes, (self.ip, self.data_port))

    def send_ds_input_packets(self, deviation_u, packet_num: int) -> None:
        """Send digital shadow bypass input to CCU (3 segment packets).

        Args:
            deviation_u: numpy array of shape (SAMPLES_PER_PACKET, LINE_SEGMENTS)
                         containing float32 deviation_U values.
            packet_num: packet number for header.
        """
        import numpy as np
        data = np.asarray(deviation_u, dtype=np.float32)
        if data.shape != (SAMPLES_PER_PACKET, LINE_SEGMENTS):
            raise ValueError(
                f"deviation_u must be ({SAMPLES_PER_PACKET}, {LINE_SEGMENTS}), "
                f"got {data.shape}")
        for seg in range(LINE_SEGMENTS):
            header = DS_RESULT_HEADER.pack(PACKET.DS_RESULT, packet_num & 0xFFFF, seg)
            payload = header + data[:, seg].tobytes()
            crc_val = crc16_ccitt(payload)
            pkt = payload + struct.pack('<H', crc_val)
            self.sock.sendto(pkt, (self.ip, self.data_port))

    def get_ccu_calibration(self, device_idx: int) -> Optional[list['ChannelInfo']]:
        """Read calibration for a specific device slot on the CCU.

        Args:
            device_idx: device index (0-3)

        Returns:
            List of ChannelInfo, or None on failure.
        """
        payload = struct.pack('B', device_idx)
        result = self.send_cmd_with_ack(CMD.GET_CCU_CALIBRATION, payload)
        if result is None:
            return None
        state, extra = result
        if len(extra) < 3:
            return None
        channels = struct.unpack_from('<H', extra, 1)[0]
        infos = []
        offset = 3
        for i in range(channels):
            if offset + STRUCT.CHANNEL.size > len(extra):
                break
            unit_bytes, ch_offset, ch_gain = STRUCT.CHANNEL.unpack(
                extra[offset:offset + STRUCT.CHANNEL.size])
            unit = unit_bytes.decode('utf-8', errors='replace').rstrip('\x00')
            infos.append(ChannelInfo(unit, ch_offset, ch_gain))
            offset += STRUCT.CHANNEL.size
        return infos

    def set_ccu_calibration(self, device_idx: int, channels: int,
                            info: list[tuple[str, float, float]]) -> bool:
        """Set calibration for a specific device slot on the CCU.

        Args:
            device_idx: device index (0-3)
            channels: number of channels
            info: list of (unit_str, offset, gain) per channel

        Returns:
            True on success.
        """
        payload = struct.pack('<BH', device_idx, channels)
        for unit, offset, gain in info:
            payload += struct.pack('<3sxff', unit.encode('utf-8')[:3], offset, gain)
        result = self.send_cmd_with_ack(CMD.SET_CCU_CALIBRATION, payload)
        return result is not None

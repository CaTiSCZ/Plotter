import logging
from logger import application_logger
from async_socket import AsyncSocket
import threading
import struct
from enum import IntEnum

    # ---------------------- CMD a packety -------------------
class PACKET(IntEnum):
    ACK_packet = 0
    ID_packet = 1
    DATA_packet = 2
    TRIGGER_packet = 3

class CMD(IntEnum):
    PING = 0
    GET_ID = 1
    REGISTER_RECEIVER = 2
    REMOVE_RECEIVER = 3
    GET_RECEIVERS =	4
    START_SAMPLING = 5
    START_ON_TRIGGER = 6
    STOP_SAMPLING = 7
    TRIGGER_ACK = 8
    FORSE_TRIGGER =	9


class Device:
    # ---------------------- CRC CCITT ----------------------
    @staticmethod
    def crc16_ccitt(data: bytes, start, end, poly=0x1021, crc=0xFFFF):
        for i in range (start, end):
            crc ^= data[i] << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ poly
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    def verify_crc(self, packet):
        if (l:=len(packet)) < 2:
            self._logger.error(f"Verify crc: Too short packet ({l}).")
            return False
        crc_position = l - 2
        received_crc = self._crc_parser.unpack_from(packet, crc_position)[0]
        if (crc:=Device.crc16_ccitt(packet, 0, crc_position)) != received_crc:
            self._logger.debug(f"CRC mismatch: expected 0x{crc:04X}, received 0x{received_crc:04X}")
            return False
        return True

    def __init__(self, cmd_socket, addr):
        self._logger = logging.getLogger(__class__.__name__ if application_logger is None else f'{application_logger}.{__class__.__name__}')
        
        self.cmd_socket = cmd_socket
        self.addr = addr

        self._packet_type_parser = struct.Struct("<H")
        self._ack_packet_parser = struct.Struct("<HI")

        self._crc_parser = struct.Struct("<H")

        self.sent_commands = {}
        self.timeout = 1

        self.send_command_lock = threading.Lock()

    def packet_received (self, packet):
        if (l:=len(packet)) < 2:
            self._logger.warning(f"Packet received: Too short packet ({l}).")
            return
        packet_type = self._packet_type_parser.unpack_from(packet, 0)[0]
        match packet_type:
            case PACKET.ACK_packet:
                error, cmd = self._ack_packet_parser.unpack_from(packet, 2)
                try:
                    with self.send_command_lock:
                        timer, on_timeout, on_ack = self.sent_commands[cmd]
                        del self.sent_commands[cmd]

                    timer.cancel()
                    on_ack(error, packet[8:])
                except KeyError:
                    self._logger.info("Packet received: unexpected ACK for {cmd}")
    
    def _on_timeout(self, cmd):
        with self.send_command_lock:
            timer, on_timeout, on_ack = self.sent_commands[cmd]
            del self.sent_commands[cmd]
        on_timeout()


    def send_command(self, cmd: int, data: bytes = b'', on_timeout = None, on_ack = None):
        with self.send_command_lock:
            if on_timeout is not None and on_ack is not None:
                if cmd in self.sent_commands:
                    self._logger.warning(f"Send command: Command {cmd} in progres.")
                    return
                self.sent_commands[cmd] = (threading.Timer(self.timeout, self._on_timeout), on_timeout, on_ack)

            packet = struct.pack('<I', cmd) + data
            self.cmd_socket.sendto(packet, self.addr)

    def ping(self):
        try:
            responses = self.send_command(CMD.PING,expect_response=True)
            if responses:
                self._logger.info(f"Ping: ok")
            else:
                self._logger.warning("Ping: no response")
        except Exception as e:
            self._logger.error(f"Ping: {e}")

    def get_id(self):
        try:
            resp = self.send_command(CMD.GET_ID, expect_response=True)
            if not resp:
                self._logger.warning("Get ID: no response")
                return
            data = verify_crc(resp)
            if not data:
                self._logger.error("Get ID: CRC failed")
                return
            parsed = parse_id_packet(data)
            self.channels_count = parsed['channels_count']

            self.update_buffers(channels_count=self.channels_count, preserve_data=False)

            # Předat nový počet kanálů do vlákna
            if self.sampling_thread:
                self.sampling_thread.set_buffers(self.signal_buffer, self.error_buffer)
                self.sampling_thread.set_channels_count(self.channels_count)

            self.init_curves()

            self._logger.info(
                f"Firmware: v{parsed['fw_ver_major']}.{parsed['fw_ver_minor']}\n"
                f"Build time: {parsed['build_time']}\n"
                f"Number of channels: {self.channels_count}"
            )
        except Exception as e:
            self._logger.error(f"Get ID: {e}")

    def register_receiver(self):
        try:
            addr, port = self.register_text_edit.text().split(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<H', int(port))
            resp = self.send_command(CMD.REGISTER_RECEIVER, data, expect_response=True)

            if not resp:
                self._logger.warning("Register receiver: no response")
                return
            
            if len(resp) < 15:
                self._logger.error(f"Register receiver: ACK to short ({len(resp)} bytes)")
                return   
                
            ip = socket.inet_ntoa(resp[8:12])
            port = struct.unpack('<H', resp[12:14])[0]
            order = resp[14]

            self._logger.info(
                f"Register receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
                f"Order: {order}"
            )
        except Exception as e:
            self._logger.error(f"Registr receiver: {e}")
    def connect(self):
        self.get_id()
        self.register_receiver()

    def remove_receiver(self):
        try:
            addr, port = self.remove_text_edit.text().split(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<H', int(port))
            resp = self.send_command(CMD.REMOVE_RECEIVER, data, expect_response=True)

            if not resp:
                self._logger.warning("Remove receiver: no response")
                return
            
            if len(resp) < 14:
                self._logger.error(f"Remove receiver: ACK to short ({len(resp)} bytes)")
                return   
                
            ip = socket.inet_ntoa(resp[8:12])
            port = struct.unpack('<H', resp[12:14])[0]

            self._logger.info(
                f"Remove receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}"
            )
        except Exception as e:
            self._logger.error(f"Remove receiver: {e}")
    
    def get_receivers(self):
        try:
            resp = self.send_command(CMD.GET_RECEIVERS, expect_response=True, expected_packets=1)
            if not resp:
                self._logger.warning("Get receivers: no response")
                return
            data = resp
            receivers = []
            # Začneme za hlavičkou ACK, což jsou 8 bajtů podle předpokladu
            offset = 8
            while offset + 6 <= len(data):
                ip = socket.inet_ntoa(data[offset:offset + 4])
                port = struct.unpack('<H', data[offset + 4:offset + 6])[0]
                receivers.append(f"{ip}:{port}")
                offset += 6
            if receivers:
                self._logger.info("Registred receivers:\n" + "\n".join(receivers))
            else:
                self._logger.warning("No Registred receivers")
        except Exception as e:
            self._logger.error(f"Get receivers: {e}")
    
    def start_sampling(self):
        self.sampling_thread.received_packets = 0
        self.num_packets = self.num_packets_spinbox.value()
        data = struct.pack('<I', self.num_packets)
        if self.channels_count == 0:
            self._logger.error("Need Get ID at first")
            return

        resp = self.send_command(CMD.START_SAMPLING, data, expect_response=True)

        self._logger.info(f"Start sampling, {self.num_packets} packets")

    def start_on_trigger(self):
            self.sampling_thread.received_packets = 0
            self.num_packets = self.num_packets_spinbox.value()
            data = struct.pack('<I', self.num_packets)
            if self.channels_count == 0:
                self._logger.error("Need Get ID at first")
                return

            resp = self.send_command(CMD.START_ON_TRIGGER, data, expect_response=True)

            self._logger.info(f"Waiting on trigger, {self.num_packets} packets")

    def stop_sampling(self):
        if self.sampling_thread and self.sampling_thread.isRunning():
            self._logger.info(f"Stop sampling, received packets: {self.sampling_thread.received_packets}")
            resp = self.send_command(CMD.STOP_SAMPLING, expect_response=True)
            
            if resp and len(resp) >= 16:  # 2+2+4+8 = 16 bajtů
                packet_type, error_state, cmd_type, packets_sent = struct.unpack('<HHIQ', resp[:16])
                
                if packet_type == PACKET.ACK_packet and cmd_type == CMD.STOP_SAMPLING:
                    self._logger.debug(f"Stop sampling confirmed, packets sent by divice: {packets_sent}")
                    if packets_sent != self.sampling_thread.received_packets:
                        self._logger.warning(f"Packet from device ({packets_sent}) not equal to recv packets ({self.sampling_thread.received_packets}) ")
                else:
                    self._logger.warning(f"Stop sampling: unexpected ACK structure or CMD")
            else:
                self._logger.warning(f"Stop sampling: no or invalid ACK response")
            
            self.sampling_thread.flush_packet_buffer()
            self.update_plot_buffered()
        else:
            self._logger.info("Sampling is already stopped")
 
    def send_trigger(self):
        try:
            resp = self.send_command(CMD.FORSE_TRIGGER)
            self._logger.info("Sent trigger (CMD 9)")
        
        except Exception as e:
            self._logger.error(f"Trigger send: {e}")
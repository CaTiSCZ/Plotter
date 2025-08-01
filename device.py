import logging
from logger import application_logger
from async_socket import AsyncSocket

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
    def __init__(self):
        self._logger = logging.getLogger(__class__.__name__ if application_logger is None else f'{application_logger}.{__class__.__name__}')
    def send_command(self, cmd: int, data: bytes = b'', expect_response: bool = False, expected_packets: int = 1):
        pkt = struct.pack('<I', cmd) + data
        self.udp_relay.sendto(pkt, (self.udp_device_addr, self.udp_device_port)) 

        if not expect_response:
            return None

        responses = []
        self.udp_relay.settimeout(0.3)
        try:
            for _ in range(expected_packets):
                resp, _ = self.udp_relay.recvfrom(1024) 
                responses.append(resp)
        except socket.timeout: #???
            if not responses:
                self._logger.warning(f"CMD {cmd}: no response")
            else:
                self._logger.info(f"CMD {cmd}: received {len(responses)} / {expected_packets} packets")
        #finally:
            #self.sock.settimeout(None)
        return responses if expected_packets > 1 else (responses[0] if responses else None)

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
import logging
from logger import application_logger
from device import Device
from event import Event

class DeviceManager:
    SAMPLES_PER_PACKET = 200
    PACKET_RATE_HZ = 1000 
    BUFFER_LENGTH_SECONDS = 10 
    def __init__(self, cmd_socket, data_socket):
        self._logger = logging.getLogger(__class__.__name__ if application_logger is None else f'{application_logger}.{__class__.__name__}')
        self._logger.debug("Device manager start")
        
        self._cmd_socket = cmd_socket
        self._data_socket = data_socket
        self._cmd_socket.register(self._packet_handler)   
        self._data_socket.register(self._packet_handler)
        self._devices = {}

        self.event_device_added = Event()
        #self.event_device_removed = Event()

    def _packet_handler(self, socket, data, addr):
        try:
            device = self._devices[addr]
            device.packet_received(data)
        except KeyError:
            self._logger.warning(f"Packet handler:Received packet from unknown device: {addr}")

    def add_device(self, addr):
        device = Device(self._cmd_socket, addr)
        self._devices[addr] = device
        self._logger.info(f"Device added: {addr}")
        self.event_device_added.emit(self, device)

    def get_data(self):
        data = []
        max_t = 0
        min_t = float('inf')
        for device in self._devices.values():
            dev = device.get_data()
            data.append(dev)
            max_t = max(max_t, dev[1])
            min_t = min(min_t, dev[1]-dev[2])

        

    def ping(self):
        for device in self._devices.values():
            device.ping()

    def get_id(self):
        for device in self._devices.values():
            device.get_id()

    def register_receiver(self, addr, source):
        for device in self._devices.values():
            device.register_receiver(addr, source)

    def connect(self):
        pass

    def remove_receiver(self, addr, source):
        for device in self._devices.values():
            device.remove_receiver(addr, source)

    def get_receivers(self, source):
        for device in self._devices.values():
            device.get_receivers(source)

    def start_sampling(self, packet_count):
        for device in self._devices.values():
            device.start_sampling(packet_count)

    def start_on_trigger(self, packet_count):
        for device in self._devices.values():
            device.start_on_trigger(packet_count)

    def stop_sampling(self):
        for device in self._devices.values():
            device.stop_sampling()
 
    def send_trigger(self):
        for device in self._devices.values():
            device.send_trigger()
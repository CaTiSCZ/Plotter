import logging
import threading
from logger import application_logger
from device import Device
from event import Event
import numpy as np
import time

class DeviceManager:
    SAMPLES_PER_PACKET = 200
    PACKET_RATE_HZ = 1000
    SAMPLE_RATE_HZ = SAMPLES_PER_PACKET * PACKET_RATE_HZ
    BUFFER_LENGTH_SECONDS = 10 
    def __init__(self, cmd_socket, data_socket):
        self._logger = logging.getLogger(__class__.__name__ if application_logger is None else f'{application_logger}.{__class__.__name__}')
        self._logger.debug("Device manager start")

        
        self._cmd_socket = cmd_socket
        self._data_socket = data_socket
        self._cmd_socket.register(self._packet_handler)   
        self._data_socket.register(self._packet_handler)
        self._devices = {}

        self.data_timeout_processor_running = True
        self.data_timeout_processor = threading.Thread(target=self._data_timeout_handler)
        self.data_timeout_processor.start()

        self.event_device_added = Event()
        #self.event_device_removed = Event()

    def _packet_handler(self, socket, data, addr):
        try:
            device = self._devices[addr]
            device.packet_received(data)
        except KeyError:
            self._logger.warning(f"Packet handler:Received packet from unknown device: {addr}")

    def _data_timeout_handler(self):    
        while self.data_timeout_processor_running:
            for device in self._devices.values():
                device.sampling_finished()
            time.sleep(0.5)

    def add_device(self, addr):
        device = Device(self._cmd_socket, addr)
        self._devices[addr] = device
        self._logger.info(f"Device added: {addr}")
        self.event_device_added.emit(self, device)

    def channels_unit(self):
        units = []
        for name, device in self._devices.items():
            # Kontrola, jestli je zařízení správně inicializované
            if device.channels_count is None or not device.channel_info:
                continue
            for channel in range(device.channels_count):
                if channel < len(device.channel_info):
                    unit = device.channel_info[channel].unit
                    units.append((unit, f"{name}.ch{channel}[{unit}]"))
                else:
                    self._logger.critical(f"Zařízení {name} nemá informace o kanálu {channel}")
        return units

    def get_data(self):
        data = []
        channels_plot = []
        max_t = 0
        min_t = float('inf')
        for device in self._devices.values():
            dev = device.get_data()
            data.append(dev)
            max_t = max(max_t, dev[1])
            min_t = min(min_t, dev[1]-dev[2])
        
        # Pokud se nepodařilo získat žádná data
        if not data or min_t == float('inf') or max_t <= min_t:
            self._logger.debug(f"Žádná validní data: data={len(data)}, min_t={min_t}, max_t={max_t}")
            return []

        time = np.arange(min_t/self.SAMPLE_RATE_HZ, max_t/self.SAMPLE_RATE_HZ, 1/self.SAMPLE_RATE_HZ)
        for device in data:
            start_index = int(device[1] - device[2] - min_t)
            end_index = int(device[1] - min_t)
            t = time[start_index:end_index]
            for channel in range(device[3]):
                channels_plot.append((t, device[0][channel, :], device[4]))
        return channels_plot

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
    
    def reset_counters(self):
        for device in self._devices.values():
            device.reset_counters()

    def stop(self):
        self.data_timeout_processor_running = False
        self.data_timeout_processor.join()
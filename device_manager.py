import logging
from logger import application_logger

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
         
    
    def _packet_handler(self, socket, data, addr):
        pass    

    def ping(self):
        pass

    def get_id(self):
        pass

    def register_receiver(self):
        pass
    def connect(self):
        pass

    def remove_receiver(self):
        pass
    
    def get_receivers(self):
        pass
    
    def start_sampling(self):
        pass

    def start_on_trigger(self):
        pass

    def stop_sampling(self):
        pass
 
    def send_trigger(self):
        pass
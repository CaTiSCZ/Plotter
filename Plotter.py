import socket
import struct
import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget, QPushButton, QGridLayout, QApplication, QSpinBox, QDoubleSpinBox, QCheckBox, QTextEdit, QScrollArea, QLineEdit, QDesktopWidget, QHBoxLayout, QSizePolicy 
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

from collections import deque, OrderedDict
import threading
import time


# ---------------------- Parametry ----------------------

UDP_DEVICE_IP =  "127.0.0.1" # "192.168.2.10"

UDP_PORT_SEND = 10578  # generátor
UDP_PORT_RECV = 10579  # tento klient - port pro příjem ACK +
UDP_PORT_DATA = 10577  # port pro příjem dat 

NUM_PACKETS = 10      # počet vzorků (požadavek v CMD 5)
RECV_TIMEOUT = 2.0
SAMPLES_PER_PACKET = 200
PACKET_RATE_HZ = 1000     # 1 paket/ms (1000 za s)
SAMPLING_PERIOD = 1/SAMPLES_PER_PACKET/PACKET_RATE_HZ # 1 packet/s, 200 vzorků/packet = 200 vzorků/ms
BUFFER_LENGTH_S = 10   # délka bufferu v s
BUFFER_SIZE = int ( BUFFER_LENGTH_S * SAMPLES_PER_PACKET * PACKET_RATE_HZ )
SIGNAL_TYPE = np.int16

# ---------------------- CMD a packety -------------------
ACK_packet = 0
ID_packet = 1
DATA_packet = 2
TRIGGER_packet = 3

#CMD
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
# ---------------------- CRC CCITT ----------------------
def crc16_ccitt(data: bytes, poly=0x1021, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

def verify_crc(pkt):
    if len(pkt) < 2:
        return None
    data = pkt[:-2]
    received_crc = struct.unpack('<H', pkt[-2:])[0]
    if (crc:=crc16_ccitt(data)) != received_crc:
        print(f"CRC mismatch: expected 0x{crc:04X}, received 0x{received_crc:04X}")
        return None
    return data


# ----------------------Vlákno na čtení dat ----------------
class SamplingThread(QThread):
    data_ready = pyqtSignal()
    log_signal = pyqtSignal(str)
    def __init__(self, addr, port, ch, buffer_lock, signal_buffer, error_buffer, cmd_sock):
        super().__init__()
        self.channels_count = ch
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((addr, port))
        self.sock.settimeout(RECV_TIMEOUT)
        self.buffer_lock = buffer_lock
        self.signal_buffer = signal_buffer
        self.error_buffer = error_buffer
        self.running = True
        self.sock.settimeout(0.3)
        self.lock = threading.Lock() 
        self.cmd_sock = cmd_sock

        # === Nový mezibuffer pro seřazené packety ===
        self.packet_buffer = {}
        self.min_buffer_size = 90
        self.chunk_size = 30
        self.last_flushed_order = -1
        self.lost_packets_counter = 0
        self.crc_error_counter = 0
        self.received_packets = 0
        
    def set_channels_count(self, new_count):
        with self.lock:
            self.channels_count = new_count

    def get_packet_buffer_size(self):
        with self.lock:
            return len(self.packet_buffer)
        
    def run(self):
        while self.running:
            try:
                pkt, _ = self.sock.recvfrom(4096)
                self.received_packets += 1
                if len(pkt) >= 2:
                    packet_type, packet_order = struct.unpack('<HH', pkt[0:4])

                    if packet_type == DATA_packet:
                        #self.received_packets += 1
                        if len(pkt) < 4:
                            self.log_signal.emit("[ERR] Received too short packet")

                        # Parsování hlavičky (2B type + 2B číslo paketu)
                        
                        
                        data = verify_crc(pkt)
                        if not data:
                            self.crc_error_counter += 1
                            self.log_signal.emit(f" [ERR] Invalid packet[{len(pkt)}] {packet_type:04X} {packet_order:5}")
                            continue

                        # === Zahoď packet, pokud je menší než všechny již uložené ===
                        if self.packet_buffer and packet_order < min(self.packet_buffer.keys()):
                            self.log_signal.emit(f"[DROP] Packet {packet_order} is older than buffer range, dropped.")
                            continue

                        # === Přidání do packet_buffer ===
                        self.packet_buffer[packet_order] = data #možná data

                        # === Pokud máme alespoň 90, odešleme 30 nejstarších ===
                        if len(self.packet_buffer) >= self.min_buffer_size:
                            sorted_keys = sorted(self.packet_buffer.keys())
                            
                            to_flush = sorted_keys[:self.chunk_size]

                            self.lost_packets_counter += to_flush[-1] - (to_flush[0] + self.chunk_size -1)
                            
                            with self.lock:
                                ch_count = self.channels_count

                            # === Dekódování a přesun dat ===
                            self.process_packets(to_flush)

                            
                            self.data_ready.emit()


                    elif packet_type == TRIGGER_packet and len(pkt) >= 5:
                        # Trigger packet
                        self.received_packets -= 1
                        packet_num, sample_num = struct.unpack('<HB', pkt[2:5])
                        self.log_signal.emit(f"[TRIGGER] Trigger packet received (packet_num={packet_num}, sample_num={sample_num})")
                        self.send_trigger_ack()  

                    else:
                        print(f"[ERR] wrong packet, {pkt}")
                        
                         
            except socket.timeout:
                pass
        if self.sock:
            self.sock.close()
            print("Data socket closed.")        

    def process_packets(self, orders: list):
        with self.lock:
            ch_count = self.channels_count

        for order in orders:
            data = self.packet_buffer.pop(order)
            offset = 4

            signals = [[] for _ in range(ch_count+1)]
            errors = []

            x = [order * SAMPLES_PER_PACKET + k for k in range(SAMPLES_PER_PACKET)]
            signals[0].extend(x)

            for ch_i in range(ch_count):
                sig = struct.unpack('<' + 'h'*SAMPLES_PER_PACKET, data[offset:offset + 2*SAMPLES_PER_PACKET])
                signals[ch_i + 1].extend(sig)
                offset += 2 * SAMPLES_PER_PACKET

            for ch_i in range(ch_count):
                errors.append(data[offset])
                offset += 1

            if ch_count % 2 != 0:
                offset += 1

            with self.buffer_lock:
                self.signal_buffer[0].extend(signals[0])
                for i in range(ch_count):
                    self.signal_buffer[i+1].extend(signals[i+1])
                    self.error_buffer[i].extend([errors[i]] * SAMPLES_PER_PACKET)
    def flush_packet_buffer(self):
        if not self.packet_buffer:
            return
        sorted_keys = sorted(self.packet_buffer.keys())
        self.process_packets(sorted_keys)
        self.data_ready.emit()

    def send_trigger_ack(self):
        try:
            packet = struct.pack('<I', TRIGGER_ACK)
            self.cmd_sock.sendto(packet, (UDP_DEVICE_IP, UDP_PORT_SEND))
            self.log_signal.emit("[ACK] Trigger ACK send")
        except Exception as e:
            print(f"[ERR] Sendind Trigger ACK failed: {e}")
            


    def stop(self):
        self.running = False
        self.quit()
        self.wait()

# ---------------------- Get ID ----------------------
ID_HEADER_STRUCT = struct.Struct('<HHHBBI3I HBB I HBB 8s 30s H')

def parse_id_packet(data):
    if len(data) < ID_HEADER_STRUCT.size:
        raise ValueError("[ERR]: ID packet is short")
    unpacked = ID_HEADER_STRUCT.unpack(data[:ID_HEADER_STRUCT.size])
    return {
        'packet_type': unpacked[0],
        'state': unpacked[1],
        'hw_id': unpacked[2],
        'hw_ver_major': unpacked[3],
        'hw_ver_minor': unpacked[4],
        'mcu_serial': unpacked[5],
        'cpu_uid': (unpacked[6], unpacked[7], unpacked[8]),
        'adc_hw_id': unpacked[9],
        'adc_ver_major': unpacked[10],
        'adc_ver_minor': unpacked[11],
        'adc_serial': unpacked[12],
        'fw_id': unpacked[13],
        'fw_ver_major': unpacked[14],
        'fw_ver_minor': unpacked[15],
        'fw_config': unpacked[16].decode('ascii').rstrip('\x00'),
        'build_time': unpacked[17].decode('ascii').rstrip('\x00'),
        'channels_count': unpacked[18],
    }


# -------------------- GUI s více tlačítky ----------------------

class SignalClient(QWidget):
    data_received = pyqtSignal(int, float, bool)

    def __init__(self):
        super().__init__()
        
        self.udp_cmd_port  = UDP_PORT_SEND
        self.udp_ack_port  = UDP_PORT_RECV
        self.udp_data_port = UDP_PORT_DATA

        self.udp_device_addr = UDP_DEVICE_IP

        self.sock = None

        self.buffer_lock = threading.Lock()
        
        self.sampling_thread = None
        
        self.num_packets = 0
        self.channels_count = 0
        self.lost_packets = 0
        self.err_packets = 0

        # === Inicializace okna ===
        self.setWindowTitle("UDP Signal Client")
        screen_geometry = QDesktopWidget().availableGeometry()
        width = int(screen_geometry.width() * 0.9)
        height = int(screen_geometry.height() * 0.9)
        self.resize(width, height)

        
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        
        # === 1. řádek: GRAF ===
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.plot = self.plot_widget.addPlot(title="Signals from all chanels")
        self.plot.setLabel('bottom', 'Time', units='s')
        self.plot.setLabel('left', 'Amplitude + offset', units='')
        self.plot.enableAutoRange(x=True, y=True)
        self.plot.showGrid(x=True, y=True, alpha=0.5)
        self.plot.setMouseEnabled(x=True, y=True)
        self.layout.addWidget(self.plot_widget)

        

        # === 2. řádek ===
        row2 = QGridLayout()
# err samples
        self.data_error_label = QLabel("ERR samples\n")
        self.data_error_label.setStyleSheet("font-family: monospace; padding: 6px;")
        self.layout.addWidget(self.data_error_label)
        row2.addWidget(self.data_error_label, 0, 0, 4, 1)
        
# Packet counters
        self.lost_packets_label = QLabel("Lost packets:")
        self.lost_packets_value = QLabel("0")
        row2.addWidget(self.lost_packets_label, 0, 1)
        row2.addWidget(self.lost_packets_value, 0, 2)

        self.err_packets_label = QLabel("ERR packets:")
        self.err_packets_value = QLabel("0")
        row2.addWidget(self.err_packets_label, 1, 1)
        row2.addWidget(self.err_packets_value, 1, 2)

        self.recv_packets_label = QLabel("Recv packets:")
        self.recv_packets_value = QLabel("0")
        row2.addWidget(self.recv_packets_label, 2, 1)
        row2.addWidget(self.recv_packets_value, 2, 2)

        self.clear_err_button = QPushButton("Clear error stats")
        self.clear_err_button.clicked.connect(self.clear_error_stats)
        row2.addWidget(self.clear_err_button, 0, 3)
# X range
        self.x_range_label = QLabel("X range:")
        self.x_range_spinbox = QDoubleSpinBox()
        self.x_range_spinbox.setRange(0, BUFFER_SIZE/SAMPLES_PER_PACKET)
        self.x_range_spinbox.setValue(200)
        self.x_range_spinbox.setSuffix(" ms")
        row2.addWidget(self.x_range_label, 0, 4, alignment=Qt.AlignRight)
        row2.addWidget(self.x_range_spinbox, 0, 5, alignment=Qt.AlignLeft)


# --- Y Min ---
        self.y_min_label = QLabel("Y min:")
        self.y_min_spinbox = QDoubleSpinBox()
        self.y_min_spinbox.setRange(-1000000, 0)
        self.y_min_spinbox.setValue(-33000.0)

        row2.addWidget(self.y_min_label, 0, 6, alignment=Qt.AlignRight)
        row2.addWidget(self.y_min_spinbox, 0, 7, alignment=Qt.AlignLeft)
# --- Y Max ---
        self.y_max_label = QLabel("Y max:")
        self.y_max_spinbox = QDoubleSpinBox()
        self.y_max_spinbox.setRange(0, 1000000)
        self.y_max_spinbox.setValue(33000.0)

        row2.addWidget(self.y_max_label, 0, 8, alignment=Qt.AlignRight)
        row2.addWidget(self.y_max_spinbox, 0, 9, alignment=Qt.AlignLeft)
        
# x auto range        

        self.auto_x_range = True
        self.auto_x_range_checkbox = QCheckBox("Auto range")
        self.auto_x_range_checkbox.setChecked(True)
        self.auto_x_range_checkbox.stateChanged.connect(self.on_auto_range_changed)
        row2.addWidget(self.auto_x_range_checkbox, 0, 10, alignment=Qt.AlignCenter)

# Buffer size
        self.buffer_size_label = QLabel("Buffer size [s]:")
        self.buffer_size_spinbox = QDoubleSpinBox()
        self.buffer_size_spinbox.setRange(0.1, 60.0)
        self.buffer_size_spinbox.setValue(BUFFER_SIZE * SAMPLING_PERIOD)
        row2.addWidget(self.buffer_size_label, 0, 11, alignment=Qt.AlignRight)
        row2.addWidget(self.buffer_size_spinbox, 0, 12, alignment=Qt.AlignLeft)

# clear graf
        self.clear_button = QPushButton("Clean graf")
        self.clear_button.clicked.connect(self.clear_plot)
        row2.addWidget(self.clear_button, 0, 13)
        # ------ 3. řádek -----
# Path display (full width)
        self.path_label = QLabel("Path:")
        self.path_display = QLineEdit("C://future_path" + 40 * "/něco" + "konec")
        self.path_display.setReadOnly(True)
        self.path_display.setStyleSheet("font-family: monospace; padding: 4px;")
        self.path_display.setFrame(False)
        self.path_display.setCursorPosition(len(self.path_display.text()))
        self.path_display.setAlignment(Qt.AlignLeft)  

        self.path_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
# save buttons
        row2.addWidget(self.path_label, 1, 3, alignment=Qt.AlignRight)
        row2.addWidget(self.path_display, 1, 4, 1, 10)
        
        self.set_path_button = QPushButton("Set path")
        row2.addWidget(self.set_path_button, 2, 4)
        
        self.save_data_button = QPushButton("Save buffer")
        row2.addWidget(self.save_data_button, 2, 5)

        self.AdHoc_safe_button = QPushButton("Ad Hoc save")
        row2.addWidget(self.AdHoc_safe_button, 2, 6)

        self.layout.addLayout(row2)
        
        # === 3. a další řádky: 4 SLOUPCE ===
        grid = QGridLayout()

        # === Sloupec 0: GENERÁTOR & KLIENT IP ===
        self.generator_ip_edit = QLineEdit(f"{self.udp_device_addr}:{self.udp_cmd_port}")
        grid.addWidget(QLabel("Device address:"), 0, 0, 1, 5)
        grid.addWidget(self.generator_ip_edit, 1, 0, 1, 2)
        self.confirm_generator_button = QPushButton("Use")
        grid.addWidget(self.confirm_generator_button, 1, 2)
        self.generator_ip_edit.returnPressed.connect(self.init_sockets)
        self.confirm_generator_button.clicked.connect(self.init_sockets)

        self.connect_generator_button = QPushButton("Connect")
        grid.addWidget(self.connect_generator_button, 1, 3,1,2)
 
#ploter ports
        grid.addWidget(QLabel("Plotter ports:"), 2, 0, 1, 5)

        self.cmd_label = QLabel("CMD:")
        self.command_port_edit = QLineEdit(str(self.udp_ack_port))
        self.command_port_edit.returnPressed.connect(self.init_sockets)
        grid.addWidget(self.cmd_label, 3, 0, alignment=Qt.AlignRight)
        grid.addWidget(self.command_port_edit, 3, 1, alignment=Qt.AlignLeft)

        self.data_label = QLabel("DATA:")
        self.data_port_edit = QLineEdit(str(self.udp_data_port))
        self.data_port_edit.returnPressed.connect(self.init_sockets)
        grid.addWidget(self.data_label, 3,2, alignment=Qt.AlignRight)
        grid.addWidget(self.data_port_edit, 3, 3, alignment=Qt.AlignLeft)


        self.confirm_client_button = QPushButton("Use")
        grid.addWidget(self.confirm_client_button, 3,4)
        self.confirm_client_button.clicked.connect(self.init_sockets)


#forse trigger
        self.send_trigger_button = QPushButton("Force Trigger")
        self.send_trigger_button.clicked.connect(self.send_trigger)
        grid.addWidget(self.send_trigger_button, 4,0, 1, 2)

        self.save_on_trigger = False
        self.save_on_trigger_checkbox = QCheckBox("Save on triger")
        self.save_on_trigger_checkbox.setChecked(True)
        #self.save_on_trigger_checkbox.stateChanged.connect(dopsat funkci)
        grid.addWidget(self.save_on_trigger_checkbox, 4, 2, 1, 3,  alignment=Qt.AlignLeft)      


#trigger position
        self.num_packets_label = QLabel("Trigger position:")
        self.num_packets_spinbox = QSpinBox()
        self.num_packets_spinbox.setRange(0, BUFFER_SIZE)
        #self.num_packets_spinbox.setValue(TRIGGER_POSITION)

        grid.addWidget(self.num_packets_label, 5, 0, 1, 2, alignment=Qt.AlignRight)
        grid.addWidget(self.num_packets_spinbox, 5, 2, 1, 3)

        # === Sloupec 1: ID & Registrace ===

#ping
        self.ping_button = QPushButton("Ping")
        self.ping_button.clicked.connect(self.ping)
        grid.addWidget(self.ping_button, 1, 5, 1, 2)

        self.get_id_button = QPushButton("Get ID")
        self.get_id_button.clicked.connect(self.get_id)
        grid.addWidget(self.get_id_button, 1, 7, 1, 2)

        self.get_receivers_button = QPushButton("Get receivers")
        self.get_receivers_button.clicked.connect(self.get_receivers)
        grid.addWidget(self.get_receivers_button, 1, 9, 1, 2)

        self.register_text_edit = QLineEdit(f"0.0.0.0:{self.udp_data_port}")
        self.register_text_edit.returnPressed.connect(self.register_receiver)
        grid.addWidget(QLabel("Register receiver:"), 3, 5, 1, 3)
        grid.addWidget(self.register_text_edit, 4, 5, 1, 2)
        self.register_button = QPushButton("Register")
        self.register_button.clicked.connect(self.register_receiver)
        grid.addWidget(self.register_button, 4, 7)

        self.remove_text_edit = QLineEdit(f"0.0.0.0:{self.udp_data_port}")
        self.remove_text_edit.returnPressed.connect(self.remove_receiver)
        grid.addWidget(QLabel("Remove receiver:"), 3, 8, 1, 3)
        grid.addWidget(self.remove_text_edit, 4, 8, 1, 2)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_receiver)
        grid.addWidget(self.remove_button, 4, 10)



        # === Sloupec 2: Sampling  ===
        self.num_packets_label = QLabel("Number of packets (0 = continue):")
        self.num_packets_spinbox = QSpinBox()
        self.num_packets_spinbox.setRange(0, 10000)
        self.num_packets_spinbox.setValue(NUM_PACKETS)

        grid.addWidget(self.num_packets_label, 0, 11)
        grid.addWidget(self.num_packets_spinbox, 0, 12)

        self.start_sampling_button = QPushButton("Start sampling")
        self.start_sampling_button.clicked.connect(self.start_sampling)
        grid.addWidget(self.start_sampling_button, 1, 11, 1, 2)

        self.trigger_sampling_button = QPushButton("Start sampling on trigger")
        self.trigger_sampling_button.clicked.connect(self.start_on_trigger)
        grid.addWidget(self.trigger_sampling_button, 2, 11, 1, 2)

        self.stop_sampling_button = QPushButton("Stop sampling")
        self.stop_sampling_button.clicked.connect(self.stop_sampling)
        grid.addWidget(self.stop_sampling_button, 3, 11, 1, 2)

        # cute packets dopsat ester egg
        self.queued_packets = QLabel("Queued packets:")
        self.queued_packets_value = QLabel("0")
        grid.addWidget(self.queued_packets, 4, 11)
        grid.addWidget(self.queued_packets_value, 5, 11, alignment=Qt.AlignCenter)


        # === Sloupec 3: LOG ===
        self.log_output = QTextEdit("Log messenge:")
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet("font-family: monospace; background-color: #f8f8f8;")
        log_scroll_area = QScrollArea()
        log_scroll_area.setWidgetResizable(True)
        log_scroll_area.setWidget(self.log_output)
        grid.addWidget(log_scroll_area, 0, 13, 6, 1)  # výška přes všechny řádky

        self.layout.addLayout(grid)

        # === Signálové křivky ===
        self.curves = []
        self.signal_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count+1)]
        self.error_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count)]
        self.init_curves()

        self.init_sockets()

        self.timer = QTimer()
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update_plot_buffered)
        self.timer.start()

        # Spojení spinboxů s funkcí
        self.x_range_spinbox.valueChanged.connect(self.update_plot_buffered)
        self.y_min_spinbox.valueChanged.connect(self.update_plot_buffered)
        self.y_max_spinbox.valueChanged.connect(self.update_plot_buffered)
        self.update_plot_buffered()

    def init_curves(self):
        self.plot.clear()
        self.curves = [self.plot.plot(pen=pg.intColor(i, hues=self.channels_count)) for i in range(self.channels_count)]

    def init_sockets(self):
        self.udp_data_port = int(self.data_port_edit.text())
        self.udp_ack_port = int(self.command_port_edit.text())
        self.udp_device_addr, self.udp_cmd_port = self.generator_ip_edit.text().split(':', 1)
        self.udp_cmd_port = int(self.udp_cmd_port)
        if self.sock:
            self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((self.udp_device_addr, self.udp_cmd_port)) # abych mohl zjistit svou adresu, potřebuji se připojit na zařízení
        self.udp_my_addr = self.sock.getsockname()[0]
        self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.udp_my_addr, self.udp_ack_port))
        self.sock.connect((self.udp_device_addr, self.udp_cmd_port))
        self.sock.settimeout(RECV_TIMEOUT)
        self.log_message(f"Connected to {self.sock.getpeername()} from {self.sock.getsockname()}")

        if self.sampling_thread and self.sampling_thread.isRunning():
            self.sampling_thread.stop()
        self.sampling_thread = SamplingThread(self.udp_my_addr, self.udp_data_port, self.channels_count, self.buffer_lock, self.signal_buffer, self.error_buffer, self.sock)
        self.sampling_thread.log_signal.connect(self.log_message)
        #self.sampling_thread.data_ready.connect(self.packet_counter)
        self.sampling_thread.start()
        self.log_message(f"Listening for data on {self.sampling_thread.sock.getsockname()}")
  
    def on_auto_range_changed(self, state):
        self.auto_x_range = self.auto_x_range_checkbox.isChecked()
        
        vb = self.plot.getViewBox()
        vb.enableAutoRange(axis=vb.XAxis, enable=self.auto_x_range)

        
        if not self.auto_x_range:
            self.update_plot_buffered("manual_range_change")

    def update_plot_buffered(self, *args):
        with self.buffer_lock:
            if self.channels_count == 0 or not self.signal_buffer or not self.signal_buffer[0]:
                return

            vb = self.plot.getViewBox()
            x_range_s = self.x_range_spinbox.value() / 1000  # ve vteřinách

            x_raw = np.array(list(self.signal_buffer[0])) * SAMPLING_PERIOD
            N = len(x_raw)
            if N == 0:
                return

            if self.auto_x_range:
                x = x_raw
            else:
                # stále vykreslíme *všechna* data
                x = x_raw
                if args:  # změna spinboxu → změň rozsah
                    xmax = x_raw[-1]
                    xmin = max(0, xmax - x_range_s)
                    vb.setXRange(xmin, xmax, padding=0)

            # Aktualizace křivek
            if len(self.curves) != self.channels_count:
                self.plot.clear()
                self.curves = [self.plot.plot(pen=pg.intColor(i, hues=self.channels_count))
                            for i in range(self.channels_count)]

            for i in range(self.channels_count):
                y_raw = np.array(list(self.signal_buffer[i + 1]))
                y = y_raw[-len(x):]
                self.curves[i].setData(x[-len(y):], y)

            vb.setYRange(self.y_min_spinbox.value(), self.y_max_spinbox.value())

            # Výpočet chyb
            N_err = int(x_range_s * SAMPLES_PER_PACKET)
            channel_errors = [sum(1 for val in list(self.error_buffer[i])[-N_err:] if val != 0)
                            for i in range(self.channels_count)]
            error_text = "Errors samples:\n" + "\n".join(
                f"Channel {i}: {count}" for i, count in enumerate(channel_errors))
            self.data_error_label.setText(error_text)

            # === Aktualizace hodnot ztracených a CRC chybných paketů ===
            if self.sampling_thread:
                self.lost_packets_value.setText(str(self.sampling_thread.lost_packets_counter))
                self.err_packets_value.setText(str(self.sampling_thread.crc_error_counter))
                self.recv_packets_value.setText(str(self.sampling_thread.received_packets))
                queued = self.sampling_thread.get_packet_buffer_size()
                self.queued_packets_value.setText(str(queued))
            if self.sampling_thread.received_packets == self.num_packets:
                self.stop_sampling()
    
    
    def packet_counter (self):
        self.received_packets +=1

        # this crashes hw
        if self.received_packets == self.num_packets:
            self.stop_sampling()
    
    def clear_error_stats(self):
        if self.sampling_thread:
            self.sampling_thread.lost_packets_counter = 0
            self.sampling_thread.crc_error_counter = 0
            self.sampling_thread.received_packets = 0
            self.lost_packets_value.setText("0")
            self.err_packets_value.setText("0")
            self.recv_packets_value.setText("0")
        self.log_message("Error counters reset.")

    def log_message(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {msg}")

    def send_command(self, cmd: int, data: bytes = b'', expect_response: bool = False, expected_packets: int = 1):
        pkt = struct.pack('<I', cmd) + data
        self.sock.send(pkt)
        
        if not expect_response:
            return None

        responses = []
        self.sock.settimeout(0.3)
        try:
            for _ in range(expected_packets):
                resp, _ = self.sock.recvfrom(1024)
                responses.append(resp)
        except socket.timeout:


# je tam tohle na místě??
            if not responses:
                print(f"[TIMEOUT] CMD {cmd}: no response")
            else:
                print(f"[INFO] CMD {cmd}: received {len(responses)} / {expected_packets} pakets")
        finally:
            self.sock.settimeout(None)

        return responses if expected_packets > 1 else (responses[0] if responses else None)

    def ping(self):
        try:
            responses = self.send_command(PING,expect_response=True)
            if responses:
                self.log_message(f"[OK] Ping: ok")
            else:
                self.log_message("[WARN] Ping: no response")
        except Exception as e:
            self.log_message(f"[ERR] Ping: {e}")

    def get_id(self):
        try:
            resp = self.send_command(GET_ID, expect_response=True)
            if not resp:
                self.log_message("[ERR] Get ID: no response")
                return
            data = verify_crc(resp)
            if not data:
                self.log_message("[ERR] Get ID: CRC failed")
                return
            parsed = parse_id_packet(data)
            self.channels_count = parsed['channels_count']

            # Změnit velikost již existujících bufferů
            with self.buffer_lock:
                # Smazat původní obsah
                self.signal_buffer.clear()
                self.error_buffer.clear()
                # Přidat nové prázdné dequey podle nového počtu kanálů
                self.signal_buffer.extend(deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count + 1))
                self.error_buffer.extend(deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count))

            # Předat nový počet kanálů do vlákna
            if self.sampling_thread:
                self.sampling_thread.set_channels_count(self.channels_count)

            self.init_curves()

            self.log_message(
                f"Firmware: v{parsed['fw_ver_major']}.{parsed['fw_ver_minor']}\n"
                f"Build time: {parsed['build_time']}\n"
                f"Number of channels: {self.channels_count}"
            )
        except Exception as e:
            self.log_message(f"[ERR] Get ID: {e}")

    def register_receiver(self):
        try:
            addr, port = self.register_text_edit.text().split(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<H', int(port))
            resp = self.send_command(REGISTER_RECEIVER, data, expect_response=True)

            if not resp:
                self.log_message("[WARN] Register receiver: no response")
                return
            
            if len(resp) < 15:
                self.log_message(f"[ERR] Register receiver: ACK to short ({len(resp)} bytes)")
                return   
                
            ip = socket.inet_ntoa(resp[8:12])
            port = struct.unpack('<H', resp[12:14])[0]
            order = resp[14]

            self.log_message(
                f"[OK] Register receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
                f"Order: {order}"
            )
        except Exception as e:
            self.log_message(f"[ERR] Registr: {e}")

    def remove_receiver(self):
        try:
            addr, port = self.remove_text_edit.text().split(':', 1)
            data = socket.inet_aton(addr) + struct.pack('<H', int(port))
            resp = self.send_command(REMOVE_RECEIVER, data, expect_response=True)

            if not resp:
                self.log_message("[WARN] Remove receiver: no response")
                return
            
            if len(resp) < 14:
                self.log_message(f"[ERR] Remove receiver: ACK to short ({len(resp)} bytes)")
                return   
                
            ip = socket.inet_ntoa(resp[8:12])
            port = struct.unpack('<H', resp[12:14])[0]

            self.log_message(
                f"[OK] Remove receiver:\n"
                f"IP: {ip}\n"
                f"Port: {port}"
            )
        except Exception as e:
            self.log_message(f"[ERR] Remove: {e}")
    
    def get_receivers(self):
        try:
            resp = self.send_command(GET_RECEIVERS, expect_response=True, expected_packets=1)
            if not resp:
                self.log_message("[ERR] Get receivers - no respond")
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
                txt = "Registred receivers:\n" + "\n".join(receivers)
            else:
                txt = "No Registred receivers"
            self.log_message(txt)
        except Exception as e:
            self.log_message(f"[ERR] Get receivers: {e}")
    
    def start_sampling(self):
        self.received_packets = 0
        self.num_packets = self.num_packets_spinbox.value()
        data = struct.pack('<I', self.num_packets)
        if self.channels_count == 0:
            self.log_message("[ERR] Need Get ID at first")
            return

        resp = self.send_command(START_SAMPLING, data, expect_response=True)

        self.log_message(f"[OK] Start sampling, {self.num_packets} packets")

    def start_on_trigger(self):
            self.received_packets = 0
            self.num_packets = self.num_packets_spinbox.value()
            data = struct.pack('<I', self.num_packets)
            if self.channels_count == 0:
                self.log_message("[ERR] Need Get ID at first")
                return

            resp = self.send_command(START_ON_TRIGGER, data, expect_response=True)

            self.log_message(f"[OK] Waiting on trigger, {self.num_packets} packets")

    def stop_sampling(self):
        if self.sampling_thread and self.sampling_thread.isRunning():
            self.log_message(f"[OK] Stop sampling, received packets: {self.received_packets}")
            resp = self.send_command(STOP_SAMPLING, expect_response=True)
            
            if resp and len(resp) >= 16:  # 2+2+4+8 = 16 bajtů
                packet_type, error_state, cmd_type, packets_sent = struct.unpack('<HHIQ', resp[:16])
                
                if packet_type == ACK_packet and cmd_type == STOP_SAMPLING:
                    self.log_message(f"[OK] Stop sampling confirmed, packets sent by divice: {packets_sent}")
                else:
                    self.log_message(f"[WARN] Stop sampling: unexpected ACK structure or CMD")
            else:
                self.log_message(f"[WARN] Stop sampling: no or invalid ACK response")
            
            self.sampling_thread.flush_packet_buffer()
            self.update_plot_buffered()
        else:
            self.log_message("[INFO] Sampling is already stopped")

        
    
    def send_trigger(self):
        try:
            resp = self.send_command(FORSE_TRIGGER)
            self.log_message("[OK] Sent trigger (CMD 9)")
        
        except Exception as e:
            self.log_message(f"[ERR] Trigger send: {e}")
    
    def clear_plot(self):
            

        # Vyčistit buffery
        with self.buffer_lock:
            for buf in self.signal_buffer:
                buf.clear()
            for buf in self.error_buffer:
                buf.clear()

        # Vyčistit vykreslené křivky a graf
        self.plot.clear()
        self.curves = []

        # Vymazat text chyb / info
        self.update_plot_buffered()
        self.log_message("Graf cleaned.")

    def closeEvent(self, event):
        print("Ukončuji aplikaci...")

        if self.sampling_thread and self.sampling_thread.isRunning():
            print("Zastavuji příjem dat...")
            self.sampling_thread.stop()

        if self.sock:
            self.sock.close()
            print("Socket uzavřen.")

        self.timer.stop()
        event.accept()






# Spuštění aplikace
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    client = SignalClient()
    client.show()
    sys.exit(app.exec_())
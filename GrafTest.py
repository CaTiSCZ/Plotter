import socket
import struct
import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget, QPushButton, QGridLayout, QApplication, QSpinBox, QDoubleSpinBox, QCheckBox, QTextEdit, QScrollArea, QLineEdit, QDesktopWidget, QHBoxLayout
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from collections import deque
import threading
import time


# ---------------------- Parametry ----------------------

UDP_IP_SEND = "192.168.2.10" # "127.0.0.1"
UDP_IP_RECV = "192.168.2.10" # "127.0.0.1"

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
    if crc16_ccitt(data) != received_crc:
        return None
    return data


# ----------------------Vlákno na čtení dat ----------------
class SamplingThread(QThread):
    data_ready = pyqtSignal()
    def __init__(self, addr, port, ch, buffer_lock, signal_buffer, error_buffer):
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
    def set_channels_count(self, new_count):
        with self.lock:
            self.channels_count = new_count
    def run(self):
        while self.running:
            try:
                pkt, _ = self.sock.recvfrom(4096)
                data = verify_crc(pkt)
                if not data:
                    continue

                # Parsování hlavičky (2B type + 2B číslo paketu)
                packet_type, packet_order = struct.unpack('<HH', data[0:4])
                if packet_type != 2:
                    continue  # jen datové pakety
                
                offset = 4

                with self.lock:
                    ch_count = self.channels_count
                signals = [[] for _ in range(ch_count+1)]
                errors = []

                x = [packet_order * SAMPLES_PER_PACKET + k for k in range (SAMPLES_PER_PACKET) ]
                signals[0].extend(x)
                # Čteme data 200 vzorků na kanál
                for ch_i in range(ch_count):
                    sig = struct.unpack('<' + 'h'*SAMPLES_PER_PACKET, data[offset:offset + 2*SAMPLES_PER_PACKET])
                    signals[ch_i + 1].extend(sig)
                    offset += 2 * SAMPLES_PER_PACKET

                # Čteme parity error pro každý kanál (1 byte)
                for ch_i in range(ch_count):
                    errors.append(data[offset])
                    offset += 1

                # Padding (když je počet kanálů lichý, přidá se 1 byte)
                if ch_count % 2 != 0:
                    offset += 1

                # CRC 2B už jsme ověřili výše

                with self.buffer_lock:
                    self.signal_buffer[0].extend(signals[0])
                    for i in range(ch_count):
                        self.signal_buffer[i+1].extend(signals[i+1])
                        self.error_buffer[i].extend([errors[i]] * SAMPLES_PER_PACKET)
                self.data_ready.emit()
            except socket.timeout:
                pass
        if self.sock:
            self.sock.close()
            print("Data socket closed.")        

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
        
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((UDP_IP_RECV, UDP_PORT_RECV))
        self.sock.settimeout(RECV_TIMEOUT)

        self.buffer_lock = threading.Lock()
        
        self.sampling_thread = None
        self.received_packets = 0
        self.num_packets = 0
        self.channels_count = 0

        
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

        self.data_error_label = QLabel("ERR packets\n")
        self.data_error_label.setStyleSheet("font-family: monospace; padding: 6px;")
        self.layout.addWidget(self.data_error_label)
        row2.addWidget(self.data_error_label, 0, 0)

        # --- X Range ---
        x_range_widget = QWidget()
        x_range_layout = QHBoxLayout()
        x_range_layout.setContentsMargins(0, 0, 0, 0)  # bez okrajů
        x_range_label = QLabel("X range:")
        self.x_range_spinbox = QDoubleSpinBox()
        self.x_range_spinbox.setRange(0, BUFFER_SIZE/SAMPLES_PER_PACKET)
        self.x_range_spinbox.setValue(200)
        self.x_range_spinbox.setSuffix(" ms")
        x_range_layout.addWidget(x_range_label)
        x_range_layout.addWidget(self.x_range_spinbox)
        x_range_widget.setLayout(x_range_layout)
        row2.addWidget(x_range_widget, 0, 1, alignment=Qt.AlignCenter)

        # --- Y Min ---
        y_min_widget = QWidget()
        y_min_layout = QHBoxLayout()
        y_min_layout.setContentsMargins(0, 0, 0, 0)
        y_min_label = QLabel("Y min:")
        self.y_min_spinbox = QDoubleSpinBox()
        self.y_min_spinbox.setRange(-1000000, 0)
        self.y_min_spinbox.setValue(-33000.0)
        y_min_layout.addWidget(y_min_label)
        y_min_layout.addWidget(self.y_min_spinbox)
        y_min_widget.setLayout(y_min_layout)
        row2.addWidget(y_min_widget, 0, 2, alignment=Qt.AlignCenter)

        # --- Y Max ---
        y_max_widget = QWidget()
        y_max_layout = QHBoxLayout()
        y_max_layout.setContentsMargins(0, 0, 0, 0)
        y_max_label = QLabel("Y max:")
        self.y_max_spinbox = QDoubleSpinBox()
        self.y_max_spinbox.setRange(0, 1000000)
        self.y_max_spinbox.setValue(33000.0)
        y_max_layout.addWidget(y_max_label)
        y_max_layout.addWidget(self.y_max_spinbox)
        y_max_widget.setLayout(y_max_layout)
        row2.addWidget(y_max_widget, 0, 3, alignment=Qt.AlignCenter)
        
        

        self.auto_x_range = True
        self.auto_x_range_checkbox = QCheckBox("Auto range")
        self.auto_x_range_checkbox.setChecked(True)
        self.auto_x_range_checkbox.stateChanged.connect(self.on_auto_range_changed)
        row2.addWidget(self.auto_x_range_checkbox, 0, 4, alignment=Qt.AlignCenter)
       
        self.clear_button = QPushButton("Clean graf")
        self.clear_button.clicked.connect(self.clear_plot)
        row2.addWidget(self.clear_button, 0, 5)

        self.layout.addLayout(row2)
        
        # === 3. a další řádky: 4 SLOUPCE ===
        grid = QGridLayout()

        # === Sloupec 0: GENERÁTOR & KLIENT IP ===
        self.generator_ip_edit = QLineEdit(f"{UDP_IP_SEND}:{UDP_PORT_SEND}")
        grid.addWidget(QLabel("Device address:"), 0, 0)
        grid.addWidget(self.generator_ip_edit, 1, 0)
        self.confirm_generator_button = QPushButton("Overwrite")
        grid.addWidget(self.confirm_generator_button, 2, 0)
    

        # Label přes celý řádek
        grid.addWidget(QLabel("Plotter ports:"), 3, 0)

        # Vnitřní widget a layout pro dva porty vedle sebe
        client_ports_widget = QWidget()
        client_ports_layout = QHBoxLayout()
        client_ports_layout.setContentsMargins(0, 0, 0, 0)
        client_ports_layout.setSpacing(10)
        client_ports_layout.setAlignment(Qt.AlignCenter)

        # CMD port: label + QLineEdit
        cmd_label = QLabel("CMD:")
        cmd_label.setAlignment(Qt.AlignCenter)
        self.command_port_edit = QLineEdit(str(UDP_PORT_RECV))
        self.command_port_edit.setMaximumWidth(80)
        self.command_port_edit.setAlignment(Qt.AlignCenter)

        # DATA port: label + QLineEdit
        data_label = QLabel("DATA:")
        data_label.setAlignment(Qt.AlignCenter)
        self.data_port_edit = QLineEdit(str(UDP_PORT_DATA))
        self.data_port_edit.setMaximumWidth(80)
        self.data_port_edit.setAlignment(Qt.AlignCenter)

        # Přidat všechny prvky do jednoho řádku
        client_ports_layout.addWidget(cmd_label)
        client_ports_layout.addWidget(self.command_port_edit)
        client_ports_layout.addSpacing(20)  # mezera mezi CMD a DATA
        client_ports_layout.addWidget(data_label)
        client_ports_layout.addWidget(self.data_port_edit)

        client_ports_widget.setLayout(client_ports_layout)
        grid.addWidget(client_ports_widget, 4, 0)

        # Tlačítko pod tím zůstává
        self.confirm_client_button = QPushButton("Overwrite")
        grid.addWidget(self.confirm_client_button, 5, 0)


        self.ping_button = QPushButton("Ping (CMD 0)")
        self.ping_button.clicked.connect(self.ping)
        grid.addWidget(self.ping_button, 6, 0)

        # === Sloupec 1: ID & Registrace ===
        self.get_id_button = QPushButton("Get ID(CMD 1)")
        self.get_id_button.clicked.connect(self.get_id)
        grid.addWidget(self.get_id_button, 0, 1)

        self.register_text_edit = QLineEdit(f"{UDP_IP_RECV}:{UDP_PORT_DATA}")
        grid.addWidget(QLabel("Register receiver:"), 1, 1)
        grid.addWidget(self.register_text_edit, 2, 1)
        self.register_button = QPushButton("Register (CMD 2)")
        self.register_button.clicked.connect(self.register_receiver)
        grid.addWidget(self.register_button, 3, 1)

        self.remove_text_edit = QLineEdit("IP:PORT")
        grid.addWidget(QLabel("Remove receiver:"), 4, 1)
        grid.addWidget(self.remove_text_edit, 5, 1)
        self.remove_button = QPushButton("Remove (CMD 3)")
        grid.addWidget(self.remove_button, 6, 1)

        self.get_receivers_button = QPushButton("Get receivers CMD 4")
        self.get_receivers_button.clicked.connect(self.get_receivers)
        grid.addWidget(self.get_receivers_button, 7, 1)

        # === Sloupec 2: Sampling & Cesta ===
        self.save_data_button = QPushButton("Save data")
        grid.addWidget(self.save_data_button, 0, 2)

        self.save_path_label = QLabel("path:")
        grid.addWidget(self.save_path_label, 1, 2)

        self.num_packets_label = QLabel("Number of packets (0 = continue):")
        self.num_packets_spinbox = QSpinBox()
        self.num_packets_spinbox.setRange(0, 10000)
        self.num_packets_spinbox.setValue(NUM_PACKETS)

        # Oba widgety do stejného sloupce
        grid.addWidget(self.num_packets_label, 2, 2)
        grid.addWidget(self.num_packets_spinbox, 3, 2)

        self.start_sampling_button = QPushButton("Start sampling (CMD 5)")
        self.start_sampling_button.clicked.connect(self.start_sampling)
        grid.addWidget(self.start_sampling_button, 4, 2)

        self.trigger_sampling_button = QPushButton("Start sampling on trigger (CMD 6)")
        grid.addWidget(self.trigger_sampling_button, 5, 2)

        self.stop_sampling_button = QPushButton("Stop sampling (CMD 7)")
        self.stop_sampling_button.clicked.connect(self.stop_sampling)
        grid.addWidget(self.stop_sampling_button, 6, 2)

        # === Sloupec 3: LOG ===
        self.log_output = QTextEdit("Log messenge:")
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet("font-family: monospace; background-color: #f8f8f8;")
        log_scroll_area = QScrollArea()
        log_scroll_area.setWidgetResizable(True)
        log_scroll_area.setWidget(self.log_output)
        grid.addWidget(log_scroll_area, 0, 3, 8, 1)  # výška přes všechny řádky

        self.layout.addLayout(grid)

        # === Signálové křivky ===
        self.curves = []
        self.signal_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count+1)]
        self.error_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count)]
        self.init_curves()

        addr = self.sock.getpeername()
        self.log_message(f"Connecter to {addr}")
        addr = self.sock.getsockname()
        self.sampling_thread = SamplingThread(addr[0], UDP_PORT_DATA, self.channels_count, self.buffer_lock, self.signal_buffer, self.error_buffer)
        self.sampling_thread.data_ready.connect(self.packet_counter)  

        self.sampling_thread.start()
        addr = self.sampling_thread.sock.getsockname()
        self.log_message(f"Listening for data on {addr}")

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


    
  
    def packet_counter (self):
        self.received_packets +=1

        if self.received_packets == self.num_packets:
            self.stop_sampling()
    
    def log_message(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {msg}")

    # ------------------- Odesílání příkazů ----------------------
    def send_command(self, cmd: int, data: bytes = b'', expect_response: bool = True, expected_packets: int = 1):
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
                #print(f"[RECV] {resp.hex()}")
        except socket.timeout:
            if not responses:
                print(f"[TIMEOUT] CMD {cmd}: no response")
            else:
                print(f"[INFO] CMD {cmd}: received {len(responses)} / {expected_packets} pakets")
        finally:
            self.sock.settimeout(None)

        return responses if expected_packets > 1 else (responses[0] if responses else None)

    def ping(self):
        try:
            responses = self.send_command(0)
            if responses:
                self.log_message(f"[OK] Ping: ok")
            else:
                self.log_message("[WARN] Ping: no response")
        except Exception as e:
            self.log_message(f"[ERR] Ping: {e}")

    def get_id(self):
        try:
            resp = self.send_command(1)
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
            data = socket.inet_aton(UDP_IP_RECV) + struct.pack('<H', UDP_PORT_DATA)
            resp = self.send_command(2, data, expect_response=True)

            if not resp:
                self.log_message("[WARN] Register receiver: no response")
                return
            
            if len(resp) < 15:
                self.log_message(f"[ERR] Register receiver: ACK to short ({len(resp)} bajts)")
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
    
    def get_receivers(self):
        try:
            resp = self.send_command(4, expect_response=True, expected_packets=1)
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
        data = struct.pack('<Q', self.num_packets)
        if self.channels_count == 0:
            self.log_message("[ERR] Need Get ID at first")
            return

        # Odeslat CMD 5 
        resp = self.send_command(5, data)


        self.log_message(f"[OK] Start sampling, {self.num_packets} packets")

    def stop_sampling(self):
        if self.sampling_thread and self.sampling_thread.isRunning():
            resp = self.send_command(7)
                       
            self.log_message(f"[OK] Stop sampling, {self.received_packets} packets")
            self.update_plot_buffered()
        else:
            self.log_message("[INFO] Sampling is already stopped")

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
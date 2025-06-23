import socket
import struct
import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget, QPushButton, QGridLayout, QApplication, QSpinBox, QDoubleSpinBox, QCheckBox
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from collections import deque
import threading


# ---------------------- Parametry ----------------------
UDP_IP = "127.0.0.1"
UDP_PORT_SEND = 9999  # generátor
UDP_PORT_RECV = 9998  # tento klient
NUM_PACKETS = 10      # počet vzorků (požadavek v CMD 5)
RECV_TIMEOUT = 2.0
SAMPLES_PER_PACKET = 200
PACKET_RATE_HZ = 1000     # 1 paket/ms (1000 za s)
sampling_period_ms = 1/200 # 1 packet/ms, 200 vzorků/packet = 200 vzorků/ms
BUFFER_LENGTH_S = 1   # délka bufferu v s
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
    def __init__(self, ch, sock, buffer_lock, signal_buffer, error_buffer):
        super().__init__()
        self.channels_count = ch
        self.sock = sock
        self.buffer_lock = buffer_lock
        self.signal_buffer = signal_buffer
        self.error_buffer = error_buffer
        self.running = True
        self.sock.settimeout(0.3)

    def run(self):
        while self.running:
            try:
                pkt, _ = self.sock.recvfrom(4096)
                data = verify_crc(pkt)
                if not data:
                    continue

                # Parsování hlavičky (2B type + 2B číslo paketu)
                packet_type, packet_num = struct.unpack('<HH', data[0:4])
                if packet_type != 2:
                    continue  # jen datové pakety

                offset = 4
                signals = [[] for _ in range(self.channels_count)]
                errors = []

                # Čteme data 200 vzorků na kanál
                for ch_i in range(self.channels_count):
                    sig = struct.unpack('<' + 'h'*SAMPLES_PER_PACKET, data[offset:offset + 2*SAMPLES_PER_PACKET])
                    signals[ch_i].extend(sig)
                    offset += 2 * SAMPLES_PER_PACKET

                # Čteme parity error pro každý kanál (1 byte)
                for ch_i in range(self.channels_count):
                    errors.append(data[offset])
                    offset += 1

                # Padding (když je počet kanálů lichý, přidá se 1 byte)
                if self.channels_count % 2 != 0:
                    offset += 1

                # CRC 2B už jsme ověřili výše

                with self.buffer_lock:
                    for i in range(self.channels_count):
                        self.signal_buffer[i].extend(signals[i])
                        self.error_buffer[i].extend([errors[i]] * SAMPLES_PER_PACKET)
                self.data_ready.emit()
            except socket.timeout:
                pass


    def stop(self):
        self.running = False
        self.quit()
        self.wait()


# ---------------------- Socket Setup ----------------------
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT_RECV))
sock.settimeout(RECV_TIMEOUT)

# ---------------------- Odesílání příkazů ----------------------
def send_command(cmd: int, data: bytes = b'', expect_response: bool = True, expected_packets: int = 1):
    pkt = struct.pack('<I', cmd) + data
    sock.sendto(pkt, (UDP_IP, UDP_PORT_SEND))
    

    if not expect_response:
        return None

    responses = []
    sock.settimeout(0.3)
    try:
        for _ in range(expected_packets):
            resp, _ = sock.recvfrom(1024)
            responses.append(resp)
            #print(f"[RECV] {resp.hex()}")
    except socket.timeout:
        if not responses:
            print(f"[TIMEOUT] CMD {cmd} – žádná odpověď")
        else:
            print(f"[INFO] CMD {cmd} – přijato {len(responses)} / {expected_packets} paketů")
    finally:
        sock.settimeout(None)

    return responses if expected_packets > 1 else (responses[0] if responses else None)


# ---------------------- Get ID ----------------------
ID_HEADER_STRUCT = struct.Struct('<HHHHBBIIHBB30sH')

def parse_id_packet(data):
    if len(data) < ID_HEADER_STRUCT.size:
        raise ValueError("Paket příliš krátký")
    unpacked = ID_HEADER_STRUCT.unpack(data[:ID_HEADER_STRUCT.size])
    return {
        'packet_type': unpacked[0],
        'state': unpacked[1],
        'hw_id': unpacked[2],
        'hw_ver_major': unpacked[3],
        'hw_ver_minor': unpacked[4],
        'hw_mcu_serial': unpacked[6],
        'hw_adc_serial': unpacked[7],
        'fw_id': unpacked[8],
        'fw_ver_major': unpacked[9],
        'fw_ver_minor': unpacked[10],
        'build_time': unpacked[11].decode('ascii').strip('\x00'),
        'channels_count': unpacked[12]
    }


# -------------------- GUI s více tlačítky ----------------------

class SignalClient(QWidget):
    data_received = pyqtSignal(int, float, bool)
    
    def __init__(self):
        super().__init__()
        self.sampling_thread = None
        self.received_packets = 0
        self.num_packets = 0

        self.buffer_lock = threading.Lock()
        self.signal_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(32)]  # max 32 kanálů
        self.error_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(32)]


        self.setWindowTitle("UDP Signálový klient")
        self.resize(1600, 1200)

        self.channels_count = 0  # počet kanálů
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)


        self.plot_widget = pg.GraphicsLayoutWidget(title="Signály")
        self.plot = self.plot_widget.addPlot(title="Signály ze všech kanálů")
   
        self.plot.setLabel('bottom', 'Čas', units='ms')
        self.plot.setLabel('left', 'Amplituda + offset', units='')
        self.plot.showAxis('bottom', show=True)
        self.plot.showAxis('left', show=True)
        self.plot.setMouseEnabled(x=True, y=True)  # povolit zoom/pan
        self.layout.addWidget(self.plot_widget) 

        self.plot.enableAutoRange(x=True, y=True)
        self.plot.showGrid(x=True, y=True, alpha=0.5)

        # Nastavení vzhledu hlavních a vedlejších čar
        self.plot.getAxis('left').setGrid(255)
        self.plot.getAxis('bottom').setGrid(255)

        self.error_label = QLabel("Chyby:\n")
        self.error_label.setStyleSheet("font-family: monospace; padding: 6px;")
        self.layout.addWidget(self.error_label)
        # ------------ osy ------------- 
       
        # Skupina pro nastavení rozsahu osy X
        self.auto_x_range = True
        self.channels_counteckbox_auto_range = QCheckBox("Automatický rozsah X")
        self.channels_counteckbox_auto_range.setChecked(True)
        self.channels_counteckbox_auto_range.stateChanged.connect(self.on_auto_range_changed)

        self.x_min_spinbox = QDoubleSpinBox()
        self.x_min_spinbox.setRange(-10000, 10000)
        self.x_min_spinbox.setValue(0)
        self.x_min_spinbox.setSuffix(" ms")

        self.x_max_spinbox = QDoubleSpinBox()
        self.x_max_spinbox.setRange(-10000, 10000)
        self.x_max_spinbox.setValue(200)
        self.x_max_spinbox.setSuffix(" ms")

        # Skupina pro Y osu
        self.y_min_spinbox = QDoubleSpinBox()
        self.y_min_spinbox.setRange(-1000000, 0)
        self.y_min_spinbox.setValue(-33000.0)

        self.y_max_spinbox = QDoubleSpinBox()
        self.y_max_spinbox.setRange(0, 1000000)
        self.y_max_spinbox.setValue(33000.0)

        # Přidání do layoutu
        self.layout.addWidget(QLabel("X min:"))
        self.layout.addWidget(self.x_min_spinbox)
        self.layout.addWidget(QLabel("X max:"))
        self.layout.addWidget(self.x_max_spinbox)
        self.layout.addWidget(QLabel("Y min:"))
        self.layout.addWidget(self.y_min_spinbox)
        self.layout.addWidget(QLabel("Y max:"))
        self.layout.addWidget(self.y_max_spinbox)

        # Připojení událostí
        self.x_min_spinbox.valueChanged.connect(self.update_plot_range)
        self.x_max_spinbox.valueChanged.connect(self.update_plot_range)
        self.y_min_spinbox.valueChanged.connect(self.update_plot_range)
        self.y_max_spinbox.valueChanged.connect(self.update_plot_range)

        # Nastavení výchozího rozsahu plochy
        self.update_plot_range()


        # ---------------- Tlačítka -----------------------------
        self.buttons_layout = QGridLayout()

        self.ping_button = QPushButton("1. Ping")
        self.ping_button.clicked.connect(self.ping)
        self.buttons_layout.addWidget(self.ping_button, 0, 0)

        self.get_id_button = QPushButton("2. Get ID")
        self.get_id_button.clicked.connect(self.get_id)
        self.buttons_layout.addWidget(self.get_id_button, 1, 0)

        self.register_button = QPushButton("3. Register receiver")
        self.register_button.clicked.connect(self.register_receiver)
        self.buttons_layout.addWidget(self.register_button, 2, 0)

        self.get_receivers_button = QPushButton("4. Get receivers")
        self.get_receivers_button.clicked.connect(self.get_receivers)
        self.buttons_layout.addWidget(self.get_receivers_button, 0, 1)

        self.start_sampling_button = QPushButton("5. Start sampling")
        self.start_sampling_button.clicked.connect(self.start_sampling)
        self.buttons_layout.addWidget(self.start_sampling_button, 1, 1)

        self.stop_sampling_button = QPushButton("7. Stop sampling")
        self.stop_sampling_button.clicked.connect(self.stop_sampling)
        self.buttons_layout.addWidget(self.stop_sampling_button, 2, 1)

        self.clear_button = QPushButton("Clean")
        self.clear_button.clicked.connect(self.clear_plot)
        self.buttons_layout.addWidget(self.clear_button, 3, 0)

        self.num_packets_spinbox = QSpinBox()
        self.num_packets_spinbox.setRange(0, 10000)
        self.num_packets_spinbox.setValue(NUM_PACKETS)
        self.num_packets_spinbox.setSuffix(" paketů (0 = kontinuálně)")
        self.buttons_layout.addWidget(self.num_packets_spinbox, 3, 1)

        self.auto_x_range_checkbox = QCheckBox("Auto rozsah osy X")
        self.auto_x_range_checkbox.setChecked(True)
        self.auto_x_range_checkbox.stateChanged.connect(self.on_auto_range_changed)
        self.buttons_layout.addWidget(self.auto_x_range_checkbox)

        self.layout.addLayout(self.buttons_layout)
        

        self.curves = []
        
        
    def update_plot_continuous(self, signals, errors):
        self.plot.clear()
        self.curves = [self.plot.plot(pen=pg.intColor(i, hues=self.channels_count)) for i in range(self.channels_count)]

        
        min_len = min(len(sig) for sig in signals)
        x = np.linspace(0, min_len * sampling_period_ms, min_len, endpoint=False)

        OFFSET = 1000
        for i in range(self.channels_count):
            shifted = np.array(signals[i][:min_len]) + i * OFFSET
            self.curves[i].setData(x, shifted)

        channel_errors = [sum(1 for val in err if val != 0) for err in errors]
        error_text = "Chyby:\n" + "\n".join(f"Kanál {i}: {count}" for i, count in enumerate(channel_errors))
        self.error_label.setText(error_text)

    def init_curves(self):
        self.plot.clear()
        self.curves = [self.plot.plot(pen=pg.intColor(i, hues=self.channels_count)) for i in range(self.channels_count)]
    
    def update_plot_range(self):
        x_min = self.x_min_spinbox.value()
        x_max = self.x_max_spinbox.value()
        y_min = self.y_min_spinbox.value()
        y_max = self.y_max_spinbox.value()

        # Získání pohledu (ViewBox) ze samotného plottovacího objektu
        vb = self.plot.getViewBox()
        vb.setXRange(x_min, x_max)
        vb.setYRange(y_min, y_max)

    def on_auto_range_changed(self, state):
        auto_x = self.auto_x_range_checkbox.isChecked()

        vb = self.plot.getViewBox()
        vb.enableAutoRange(axis=vb.XAxis, enable=auto_x)

        if not auto_x:
            self.update_plot_range()
    
    def update_plot_buffered(self, signals=None, errors=None, *args):
        with self.buffer_lock:
            if self.channels_count == 0 or not self.signal_buffer or not self.signal_buffer[0]:
                return
            if signals is None or errors is None:
                # fallback - použij buffery v self
                signals = self.signal_buffer
                errors = self.error_buffer
            # Vykreslíme posledních N vzorků
            N = min(BUFFER_SIZE, min(len(buf) for buf in self.signal_buffer))
            if N == 0:
                return

            
            x = np.linspace(-N * sampling_period_ms, 0, N, endpoint=False)

            if len(self.curves) != self.channels_count:
                self.plot.clear()
                self.curves = [self.plot.plot(pen=pg.intColor(i, hues=self.channels_count)) for i in range(self.channels_count)]

            OFFSET = 1000
            for i in range(self.channels_count):
                data = np.array(list(self.signal_buffer[i])[-N:])
                shifted = data + i * OFFSET
                self.curves[i].setData(x, shifted)

            if self.auto_x_range:
                # Automaticky podle dat
                self.plot.setXRange(-N * sampling_period_ms, 0)
            else:
                # Manuální rozsah
                self.plot.setXRange(self.x_min_spinbox.value(), self.x_max_spinbox.value())

            channel_errors = [sum(1 for val in list(self.error_buffer[i])[-N:] if val != 0) for i in range(self.channels_count)]
            error_text = "Chyby (posledních vzorků):\n" + "\n".join(f"Kanál {i}: {count}" for i, count in enumerate(channel_errors))
            self.error_label.setText(error_text)

    def packet_counter (self):
        self.received_packets +=1

        if self.received_packets == self.num_packets:
            self.stop_sampling()
    

    def ping(self):
        try:
            responses = send_command(0)
            if responses:
                self.error_label.setText(f"[OK] Ping úspěšný")
            else:
                self.error_label.setText("[WARN] Ping bez odpovědi")
        except Exception as e:
            self.error_label.setText(f"[ERR] Ping: {e}")

    def get_id(self):
        try:
            resp = send_command(1)
            if not resp:
                self.error_label.setText("[ERR] Get ID: žádná odpověď")
                return
            data = verify_crc(resp)
            if not data:
                self.error_label.setText("[ERR] Get ID: CRC selhalo")
                return
            parsed = parse_id_packet(data)
            self.channels_count = parsed['channels_count']
            txt = (
                f"Firmware: v{parsed['fw_ver_major']}.{parsed['fw_ver_minor']}\n"
                f"Build time: {parsed['build_time']}\n"
                f"Počet kanálů: {self.channels_count}"
            )
            self.error_label.setText(txt)
        except Exception as e:
            self.error_label.setText(f"[ERR] Get ID: {e}")

    def register_receiver(self):
        try:
            data = socket.inet_aton(UDP_IP) + struct.pack('<H', UDP_PORT_RECV)
            resp = send_command(2, data, expect_response=True)

            if not resp:
                self.error_label.setText("[WARN] Registrace - žádná odpověď")
                return
            
            if len(resp) < 15:
                self.error_label.setText(f"[ERR] Registrace - odpověď příliš krátká ({len(resp)} bajtů)")
                return   
                
            ip = socket.inet_ntoa(resp[8:12])
            port = struct.unpack('<H', resp[12:14])[0]
            order = resp[14]

            self.error_label.setText(
                f"[OK] Registrován jako příjemce:\n"
                f"IP: {ip}\n"
                f"Port: {port}\n"
                f"Pořadí registrace: {order}"
            )
        except Exception as e:
            self.error_label.setText(f"[ERR] Registrace: {e}")
    
    def get_receivers(self):
        try:
            resp = send_command(4, expect_response=True, expected_packets=1)
            if not resp:
                self.error_label.setText("[ERR] Get receivers: žádná odpověď")
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
                txt = "Registrovaní příjemci:\n" + "\n".join(receivers)
            else:
                txt = "Žádní registrovaní příjemci"
            self.error_label.setText(txt)
        except Exception as e:
            self.error_label.setText(f"[ERR] Get receivers: {e}")
    
    def start_sampling(self):
        self.received_packets = 0
        self.num_packets = self.num_packets_spinbox.value()
        data = struct.pack('<Q', self.num_packets)
        if self.channels_count == 0:
            self.error_label.setText("Nejdřív zavolej Get ID")
            return

        # Nekonečné sampling
        if self.sampling_thread and self.sampling_thread.isRunning():
            self.error_label.setText("[INFO] Kontinuální příjem již běží")
            return

        # Odeslat CMD 5 
        resp = send_command(5, data)

        # Vyčistit buffery
        self.signal_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count)]
        self.error_buffer = [deque(maxlen=BUFFER_SIZE) for _ in range(self.channels_count)]
        self.init_curves()

        #self.plot.setXRange(-BUFFER_SIZE * sampling_period_ms, 0)

        self.sampling_thread = SamplingThread(self.channels_count, sock, self.buffer_lock, self.signal_buffer, self.error_buffer)
        self.sampling_thread.data_ready.connect(self.packet_counter)  
        self.sampling_thread.start()

        self.timer = QTimer()
        self.timer.setInterval(33)  # cca 30 FPS
        self.timer.timeout.connect(self.update_plot_buffered)
        self.timer.start()

        self.error_label.setText("[OK] Spuštěn kontinuální příjem a vykreslování")

    def stop_sampling(self):
        if self.sampling_thread and self.sampling_thread.isRunning():
            self.sampling_thread.stop()
            self.timer.stop()
            self.error_label.setText("[OK] Stop sampling")
            self.update_plot_buffered()
        else:
            self.error_label.setText("[INFO] Kontinuální příjem neběží")

    def clear_plot(self):
        # Zastavit průběžné vykreslování, pokud běží
        if hasattr(self, 'timer') and self.timer.isActive():
            self.timer.stop()

        # Zastavit vlákno, pokud běží
        if self.sampling_thread and self.sampling_thread.isRunning():
            self.sampling_thread.stop()

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
        self.error_label.setText("Graf vyčištěn.")







# Spuštění aplikace
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    client = SignalClient()
    client.show()
    sys.exit(app.exec_())
import socket
import struct
import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget, QPushButton, QApplication

# ---------------------- Parametry ----------------------
UDP_IP = "127.0.0.1"
UDP_PORT_SEND = 9999  # generátor
UDP_PORT_RECV = 9998  # tento klient
NUM_SAMPLES = 10      # počet vzorků (požadavek v CMD 5)
RECV_TIMEOUT = 2.0
SAMPLES_PER_PACKET = 200
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UDP Signálový klient")
        self.resize(800, 600)

        self.ch = 0  # počet kanálů
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        self.plot_widget = pg.GraphicsLayoutWidget(title="Signály")
        self.plot = self.plot_widget.addPlot(title="Signály ze všech kanálů")
        self.plot.showGrid(x=True, y=True)
        self.layout.addWidget(self.plot_widget)

        self.error_label = QLabel("Chyby:\n")
        self.error_label.setStyleSheet("font-family: monospace; padding: 6px;")
        self.layout.addWidget(self.error_label)

        # Tlačítka
        self.ping_button = QPushButton("1. Ping")
        self.ping_button.clicked.connect(self.ping)
        self.layout.addWidget(self.ping_button)

        self.get_id_button = QPushButton("2. Get ID")
        self.get_id_button.clicked.connect(self.get_id)
        self.layout.addWidget(self.get_id_button)

        self.register_button = QPushButton("3. Registruj příjemce")
        self.register_button.clicked.connect(self.register_receiver)
        self.layout.addWidget(self.register_button)

        self.get_receivers_button = QPushButton("4. Get receivers")
        self.get_receivers_button.clicked.connect(self.get_receivers)
        self.layout.addWidget(self.get_receivers_button)

        self.start_sampling_button = QPushButton("5. Start sampling a vykresli")
        self.start_sampling_button.clicked.connect(self.start_sampling)
        self.layout.addWidget(self.start_sampling_button)

        self.curves = []

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
            self.ch = parsed['channels_count']
            txt = (
                f"Firmware: v{parsed['fw_ver_major']}.{parsed['fw_ver_minor']}\n"
                f"Build time: {parsed['build_time']}\n"
                f"Počet kanálů: {self.ch}"
            )
            self.error_label.setText(txt)
        except Exception as e:
            self.error_label.setText(f"[ERR] Get ID: {e}")

    def register_receiver(self):
        try:
            data = socket.inet_aton(UDP_IP) + struct.pack('<H', UDP_PORT_RECV)
            resp = send_command(2, data=data)
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
            resp = send_command(4)
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
        if self.ch == 0:
            self.error_label.setText("Nejdřív zavolej Get ID")
            return
        try:
            n = NUM_SAMPLES
            data = struct.pack('<Q', n)
            responses = send_command(5, data=data, expected_packets=n+1)
            if not responses or len(responses) < n+1:
                self.error_label.setText(f"[ERR] Sampling: přišlo {len(responses) if responses else 0} paketů, očekáváno {n+1}")
                return

            signals = [[] for _ in range(self.ch)]
            errors = [[] for _ in range(self.ch)]

            for i, pkt in enumerate(responses[1:], start=1):  # první paket je ACK
                verified = verify_crc(pkt)
                if not verified:
                    print(f"[WARN] CRC selhalo u paketu č.{i}")
                    continue
                offset = 4
                for ch_i in range(self.ch):
                    sig = struct.unpack('<' + 'h'*SAMPLES_PER_PACKET, verified[offset:offset+400])
                    signals[ch_i].extend(sig)
                    offset += 400
                for ch_i in range(self.ch):
                    errors[ch_i].append(verified[offset])
                    offset += 1

            self.plot.clear()
            self.curves = [self.plot.plot(pen=pg.intColor(i, hues=self.ch)) for i in range(self.ch)]
            min_len = min(len(sig) for sig in signals)
            for i in range(self.ch):
                self.curves[i].setData(signals[i][:min_len])

            channel_errors = [sum(1 for val in err if val != 0) for err in errors]
            error_text = "Chyby:\n" + "\n".join(f"Kanál {i}: {count}" for i, count in enumerate(channel_errors))
            self.error_label.setText(error_text + f"\n[INFO] Přijato paketů: {len(responses)}")
        except Exception as e:
            self.error_label.setText(f"[ERR] Sampling: {e}")

# Pomocné funkce ponechány mimo GUI
ID_HEADER_STRUCT = struct.Struct('<HHHHBBIIHBB30sH')

# Spuštění aplikace
app = QApplication([])
client = SignalClient()
client.show()
app.exec_()

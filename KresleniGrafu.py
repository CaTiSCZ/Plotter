import socket
import sys
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets, QtCore
from collections import deque
from queue import Queue
import threading

# UDP nastavení
UDP_IP = "0.0.0.0"
UDP_PORT = 9999
BUFFER_SIZE = 65507
display_range = 5  # sekund
update_interval_ms = 33  # cca 1/30 sekundy

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)


data_queue = Queue()
x_data = deque()
y1_data = deque()
y2_data = deque()

# PyQtGraph a Qt setup
app = QtWidgets.QApplication([])
win = QtWidgets.QMainWindow()
central_widget = QtWidgets.QWidget()
win.setCentralWidget(central_widget)
layout = QtWidgets.QVBoxLayout(central_widget)

# Grafický widget
plot_widget = pg.GraphicsLayoutWidget()
layout.addWidget(plot_widget)

# Ovládací prvky
controls_layout = QtWidgets.QHBoxLayout()
layout.addLayout(controls_layout)

controls_layout.addWidget(QtWidgets.QLabel("Rozsah X [s]:"))

range_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
range_slider.setMinimum(1)
range_slider.setMaximum(100)
range_slider.setValue(10)  # výchozí zobrazený rozsah (v sekundách)
controls_layout.addWidget(range_slider)

range_label = QtWidgets.QLabel("10 s")  # zobrazení aktuální hodnoty
controls_layout.addWidget(range_label)

live_button = QtWidgets.QPushButton("Live View")
controls_layout.addWidget(live_button)

# Plot a osy
plot = plot_widget.addPlot(title="Napětí a proud")
plot.setLabel('left', 'Napětí [V]', color='blue')
plot.setLabel('bottom', 'Čas [s]', color='black')
plot.showGrid(x=True, y=True)

right_axis = pg.ViewBox()
plot.showAxis('right')
plot.getAxis('right').setLabel('Proud [A]', color='red')
plot.scene().addItem(right_axis)
plot.getAxis('right').linkToView(right_axis)
right_axis.setXLink(plot)

plot.setYRange(-2, 2)
right_axis.setYRange(-5, 12)
right_axis.enableAutoRange(axis='y', enable=False)

data_lock = threading.Lock()
# Data
MAX_DATA = 1000000  
x_data = deque(maxlen=MAX_DATA)
y1_data = deque(maxlen=MAX_DATA)
y2_data = deque(maxlen=MAX_DATA)

curve_y1 = plot.plot(pen='b', name="Napětí")
curve_y2 = pg.PlotDataItem(pen='r', name="Proud")
right_axis.addItem(curve_y2)

# Řízení zobrazení
display_range = 10  # sekund
live_mode = True

def update():
    if not x_data:
        return

    with data_lock:
        x_np  = np.array(x_data, dtype=float)  # nebo np.array(list(x_data))
        y1_np = np.array(y1_data, dtype=float)
        y2_np = np.array(y2_data, dtype=float)

    x_max = x_np[-1]
    x_min = x_max - display_range
    mask  = (x_np >= x_min)

    curve_y1.setData(x_np[mask], y1_np[mask])
    curve_y2.setData(x_np[mask], y2_np[mask])

    plot.setXRange(x_min, x_max, padding=0.01)
    right_axis.setXRange(x_min, x_max, padding=0.01)

# Funkce pro příjem UDP dat
def receive_data():
    print("Čekám na UDP data…")
    while True:
        try:
            data, _ = sock.recvfrom(BUFFER_SIZE)
            text = data.decode('utf‑8').strip()

            # zpracuj CELÝ paket (200 řádků) a pak jedním zámkem přidej do deque
            loc_x, loc_y1, loc_y2 = [], [], []
            for line in text.splitlines():
                try:
                    xs, vstr, istr = line.split()
                    loc_x.append(float(xs))
                    loc_y1.append(float(vstr))
                    loc_y2.append(float(istr))
                except ValueError:
                    continue            # přeskoč neplatný řádek

            with data_lock:
                x_data.extend(loc_x)
                y1_data.extend(loc_y1)
                y2_data.extend(loc_y2)

        except socket.timeout:
            continue
        except Exception as e:
            print("Chyba při příjmu:", e)

def on_slider_change():
    global display_range
    display_range = range_slider.value()
    range_label.setText(f"{display_range} s")
    update()

def on_live_button():
    global live_mode
    live_mode = True
    update()

# Event: změna rozsahu
range_slider.valueChanged.connect(on_slider_change)
live_button.clicked.connect(on_live_button)

# Synchronizace pohybu osy X mezi hlavním grafem a pravou osou
def sync_views():
    right_axis.setGeometry(plot.getViewBox().sceneBoundingRect())
    right_axis.linkedViewChanged(plot.getViewBox(), right_axis.XAxis)

plot.getViewBox().sigResized.connect(sync_views)

# Timer pro aktualizaci
timer = QtCore.QTimer()
timer.timeout.connect(update)
timer.start(30)

# Start přijímání dat
threading.Thread(target=receive_data, daemon=True).start()

# Zobrazení
win.resize(1000, 600)
win.setWindowTitle("Real-time Plotter: Napětí a proud")
win.show()
app.exec_()

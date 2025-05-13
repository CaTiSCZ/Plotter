import pyqtgraph as pg
import numpy as np
import socket
import threading
from collections import deque
from PyQt5 import QtWidgets, QtCore

# UDP nastavení
UDP_IP = "0.0.0.0"
UDP_PORT = 9999
BUFFER_SIZE = 65507

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
sock.settimeout(0.1)

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

# Data
MAX_DATA = 5*60*1000*200
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
    if len(x_data) == 0:
        return

    x_np = np.array(x_data)
    y1_np = np.array(y1_data)
    y2_np = np.array(y2_data)

    curve_y1.setData(x_np, y1_np)
    curve_y2.setData(x_np, y2_np)

    if live_mode:
        x_max = x_np[-1]
        x_min = x_max - display_range
        plot.setXRange(x_min, x_max, padding=0)
        right_axis.setXRange(x_min, x_max, padding=0)

# Funkce pro příjem UDP dat
def receive_data():
    print("Čekám na UDP data...")
    while True:
        try:
            # Čekání na UDP data
            data, addr = sock.recvfrom(BUFFER_SIZE)
            text = data.decode('utf-8').strip()

            # Zpracování příchozích dat
            for line in text.splitlines():
                try:
                    # Předpokládáme formát: x y1 y2
                    x_str, y1_str, y2_str = line.split()
                    x = float(x_str)
                    y1 = float(y1_str)
                    y2 = float(y2_str)

                    # Přidání dat do seznamu
                    x_data.append(x)
                    y1_data.append(y1)
                    y2_data.append(y2)

                    # Pokud jsou velikosti všech dat stejný, pak se křivky aktualizují
                    if len(x_data) == len(y1_data) == len(y2_data):
                        curve_y1.setData(np.array(x_data), np.array(y1_data))
                        curve_y2.setData(np.array(x_data), np.array(y2_data))

                except ValueError:
                    continue  # Pokud je formát dat neplatný, ignorujeme

        except socket.timeout:
            continue  # Ignoruje timeouty (pokud neprobíhá žádná komunikace)
        except Exception as e:
            print(f"Chyba při příjmu dat: {e}")

def on_slider_change():
    global display_range
    display_range = range_slider.value()
    # nepřepínáme zpět do live módu, jen měníme velikost zobrazené oblasti
    range_label.setText(f"{display_range} s")  # aktualizace textu

def on_live_button():
    global live_mode
    live_mode = True
    update()  # okamžitě skočí na nejnovější data

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
timer.start(100)

# Start přijímání dat
threading.Thread(target=receive_data, daemon=True).start()

# Zobrazení
win.resize(1000, 600)
win.setWindowTitle("Real-time Plotter: Napětí a proud")
win.show()
app.exec_()

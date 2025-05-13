import pyqtgraph as pg
import numpy as np
import socket
import threading
from collections import deque

# UDP nastavení
UDP_IP = "0.0.0.0"  # Nasloucháme na všech rozhraních
UDP_PORT = 9999
BUFFER_SIZE = 65507

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
sock.settimeout(0.1)

# PyQtGraph nastavení
app = pg.mkQApp("Real-time Plotter")

# Použijeme GraphicsLayoutWidget pro lepší správu rozložení
win = pg.GraphicsLayoutWidget()  # Používáme widget, který se lépe přizpůsobí velikosti
win.setWindowTitle("Real-time data")
win.resize(1000, 600)  # Nastavení velikosti okna

# Vytvoření grafu a jeho os
plot = win.addPlot(title="Napětí a proud")  # Přímo přidáme plot do layoutu
plot.setLabel('left', 'Napětí [V]', color='blue')  # Label pro osu Y1
plot.setLabel('bottom', 'Čas [s]', color='black')  # Label pro osu X

# Vytvoření druhé osy Y pro proud
right_axis = pg.ViewBox()
plot.showAxis('right')  # Zobrazí osu Y na pravé straně
plot.getAxis('right').setLabel('Proud [A]', color='red')
plot.scene().addItem(right_axis)
plot.getAxis('right').linkToView(right_axis)
right_axis.setXLink(plot)

plot.setYRange(-2, 2)  # Nastavíme rozmezí osy Y pro napětí (modrá křivka)
right_axis.setYRange(-5, 12)  # Nastavíme rozmezí osy Y pro proud (červená křivka)
right_axis.enableAutoRange(axis='y', enable=False)  # Deaktivujeme automatické rozmezí pro pravou osu Y

# Synchronizace velikosti pravé ViewBox s hlavní ViewBox
def update_views():
    right_axis.setGeometry(plot.vb.sceneBoundingRect())
    right_axis.linkedViewChanged(plot.vb, right_axis.XAxis)

plot.vb.sigResized.connect(update_views)

# Data pro grafy
rozsah = 50000
x_data = deque(maxlen=rozsah)  # Osa X (max 500 bodů)
y1_data = deque(maxlen=rozsah)  # Osa Y1 (napětí)
y2_data = deque(maxlen=rozsah)  # Osa Y2 (proud)

# Křivky pro napětí (Y1) a proud (Y2)
curve_y1 = plot.plot(pen='b', name="Napětí [V]")
curve_y2 = pg.PlotDataItem(pen='r', name="Proud [A]")
right_axis.addItem(curve_y2)

# Funkce pro aktualizaci grafu
def update():
    if len(x_data) > 0:
        # Převod dat na numpy array pro rychlost
        x_np = np.array(x_data)
        y1_np = np.array(y1_data)
        y2_np = np.array(y2_data)

        # Aktualizace dat pro křivky
        curve_y1.setData(x_np, y1_np)
        curve_y2.setData(x_np, y2_np)

        # Zajištění, že se osa X posouvá správně
        plot.setXRange(x_np[0], x_np[-1])
        right_axis.setXRange(x_np[0], x_np[-1])

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

                except ValueError:
                    continue  # Pokud je formát dat neplatný, ignorujeme

        except socket.timeout:
            continue  # Ignoruje timeouty (pokud neprobíhá žádná komunikace)
        except Exception as e:
            print(f"Chyba při příjmu dat: {e}")

# Hlavní smyčka pro příjem dat a vykreslování
try:
    # Spuštění příjmu dat v samostatném vlákně
    data_thread = threading.Thread(target=receive_data, daemon=True)
    data_thread.start()

    # Spuštění hlavního časovače pro aktualizaci grafu
    timer = pg.QtCore.QTimer()
    timer.timeout.connect(update)
    timer.start(100)  # Interval 100 ms mezi aktualizacemi

    # Zobrazení okna s grafem
    win.show()

    # Spuštění hlavní aplikace (GUI)
    app.exec_()

except KeyboardInterrupt:
    print("\nUkončeno uživatelem.")
finally:
    sock.close()
    print("Socket zavřen.")

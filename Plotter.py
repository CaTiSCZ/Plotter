import logging
import logger

DEFAULT_SOCKET_BACKEND = 'auto'  # 'auto', 'cpp', or 'py'

def _resolve_buffered_socket_class(backend: str = DEFAULT_SOCKET_BACKEND):
    backend = (backend or DEFAULT_SOCKET_BACKEND).strip().lower()
    if backend in ('auto', 'cpp'):
        try:
            import cppimport
            mod = cppimport.imp('buffered_socket.buffered_socket_cpp')
            return mod.BufferedSocket, 'cpp'
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, GeneratorExit)):
                raise
            if backend == 'cpp':
                raise
            logging.getLogger(__name__).warning(
                f"C++ buffered socket import failed ({type(e).__name__}: {e}). Falling back to Python backend."
            )
    if backend in ('auto', 'py', 'python'):
        from buffered_socket_py import BufferedSocket as PyBufferedSocket
        return PyBufferedSocket, 'python'
    raise ValueError(f"Unknown socket backend '{backend}'. Expected one of: auto, cpp, py")

from contextlib import ExitStack

import pyqtgraph as pg
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
import time

from async_socket import AsyncSocket
from device_manager import DeviceManager
from gui import Gui

# ---------------------- Logging ----------------------


# ---------------------- Výchozí adresy ----------------------

UDP_DEVICE_IP =  "127.0.0.1" # "192.168.2.10"

UDP_PORT_SEND = 10578  # generátor
UDP_PORT_RECV = 10579  # tento klient - port pro příjem ACK +
UDP_PORT_DATA = 10577  # port pro příjem dat 

# -------------------- GUI s více tlačítky ----------------------
class Plotter:
    def __init__(self):
        super().__init__()
        self.isShown = False
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')
        self._logger.debug("Plotter GUI start")

# === Sockets ===
        socket_cls, self.backend_name = _resolve_buffered_socket_class()
        self._logger.info(f"Using buffered socket backend: {self.backend_name}")
        self.cmd_socket = AsyncSocket(socket_cls(name="cmd_buffered"), name="cmd_async")
        self.data_socket = AsyncSocket(socket_cls(name="data_buffered"), name="data_async")

        self.udp_device_addr = UDP_DEVICE_IP
        self.udp_device_port = UDP_PORT_SEND #generátor
        self.udp_ack_port = UDP_PORT_RECV #klient pro ack
        self.udp_data_port = UDP_PORT_DATA #klient pro data
        self.num_packets = 0
        self.buffer_length_sec = DeviceManager.BUFFER_LENGTH_SECONDS
        self.buffer_size = int(self.buffer_length_sec * DeviceManager.SAMPLES_PER_PACKET * DeviceManager.PACKET_RATE_HZ)

        self.decimation_factor = 1
        self.decimation_mode = "mean"
        self.pen_width = 1
        self.current_time = time.time()
# === Device Manager ===
        self.device_manager = DeviceManager(self.cmd_socket, self.data_socket)

# === Křivky pro grafy ===
        self.curves_mv = []  # Křivky pro jednotku mV (levá osa)
        self.curves_a = []   # Křivky pro jednotku A (pravá osa)
        self.channel_units = []  # Ukládá jednotky kanálů v pořadí

# === inicializace grafu ===
        self.gui = Gui(self)

        self.plot_timer_running = False

    def _start_plot_timer(self):
        if not self.plot_timer_running:
            self.gui.plot_timer.start(100)  # Aktualizace každých 100ms (10 FPS)
            self.plot_timer_running = True
            self._logger.info("Plot timer started")

    def _init_curves(self):
        # Vyčistíme staré křivky
        for curve in self.curves_mv + self.curves_a:
            if curve in self.gui.plot.listDataItems():
                self.gui.plot.removeItem(curve)
            if hasattr(self, 'right_axis') and curve in self.gui.right_axis.addedItems:
                self.gui.right_axis.removeItem(curve)
        
        self.curves_mv.clear()
        self.curves_a.clear()
        
        # Získáme informace o jednotkách kanálů
        units = self.device_manager.channels_unit()
        self.channel_units = [unit[0] for unit in units]  # Ukládáme pouze jednotky
        
        colors = ['r', 'g', 'b', 'c', 'm', 'y', 'k', 'w']  # Základní barvy pro křivky
        self.last_packet_nums = [-1] * len(units)  # Ukládáme poslední čísla paketů pro každý kanál
        # Vytvoříme křivky pro každý kanál podle jeho jednotky
        for i, (unit, label) in enumerate(units):
            color = colors[i % len(colors)]
            if unit == 'mV':
                # Křivka pro napětí na levé ose
                curve = self.gui.plot.plot(pen=pg.mkPen(color, width=self.pen_width), name=label)
                self.curves_mv.append(curve)
            elif unit == 'A':
                # Křivka pro proud na pravé ose - PlotDataItem
                curve = pg.PlotDataItem(pen=pg.mkPen(color, width=self.pen_width), name=label)
                self.gui.right_axis.addItem(curve)
                self.curves_a.append(curve)
            else:
                # Pro jiné jednotky použijeme levou osu jako výchozí s čárkovaným stylem
                self._logger.warning(f"Neznámá jednotka '{unit}' pro kanál '{label}', použita levá osa s čárkovaným stylem")
                dash_pen = pg.mkPen(color, width=self.pen_width, style=2)
                curve = self.gui.plot.plot(pen=dash_pen, name=f"{label} (neznámá jednotka)")
                self.curves_mv.append(curve)
        self.decimation_changed()  # Aplikujeme decimaci na nové křivky
        self._logger.info(f"Inicializovány křivky: {len(self.curves_mv)} na levé ose (mV), {len(self.curves_a)} na pravé ose (A)")

    def _update_plot_data(self, force_update=False):
        """Aktualizuje data v grafu podle jednotek kanálů"""
        try:
            current_time = time.time()
            last_update_time = current_time - self.current_time
            self.update_time_label(current_time, last_update_time)
            self.current_time = current_time
            # Aktualizujeme statistiky paketů
            self._update_packet_stats()
            
            # Pokud nejsou inicializované křivky, neděláme nic
            if not self.curves_mv and not self.curves_a:
                return
                
            # Získáme data ze všech zařízení
            channels_data = self.device_manager.get_data()
            
            if not channels_data:
                return
            
            # Kontrola, jestli máme správný počet křivek
            total_curves = len(self.curves_mv) + len(self.curves_a)
            if len(channels_data) != total_curves:
                self._logger.warning(f"Nesoulad počtu kanálů: data={len(channels_data)}, křivky={total_curves}")
                return
            
            # Kontrola, jestli máme správný počet jednotek
            if len(self.channel_units) != len(channels_data):
                self._logger.warning(f"Nesoulad počtu jednotek: units={len(self.channel_units)}, data={len(channels_data)}")
                return
            
            # Aktualizujeme data pro křivky podle jednotek
            mv_index = 0
            a_index = 0
            is_new_data = force_update
            for i, (time_data, channel_data, last_packet_num) in enumerate(channels_data):
                # Kontrola rozměrů dat
                if len(time_data) == 0 or len(channel_data) == 0:
                    continue
                    
                if len(time_data) != len(channel_data):
                    self._logger.warning(f"Nesoulad rozměrů pro kanál {i}: time={len(time_data)}, data={len(channel_data)}")
                    continue

                if not force_update:    
                    if self.last_packet_nums[i] != last_packet_num:
                        self.last_packet_nums[i] = last_packet_num
                        is_new_data = True
                    else:
                        is_new_data = False
                
                if i < len(self.channel_units):
                    unit = self.channel_units[i]
                    
                    if unit == 'mV' and mv_index < len(self.curves_mv):
                        # Aktualizace křivky na levé ose (mV)
                        if is_new_data:
                            self.curves_mv[mv_index].setData(time_data, channel_data)
                        mv_index += 1
                    elif unit == 'A' and a_index < len(self.curves_a):
                        # Aktualizace křivky na pravé ose (A)
                        if is_new_data:
                            self.curves_a[a_index].setData(time_data, channel_data)
                        a_index += 1
                    else:
                        # Pro neznámé jednotky použijeme levou osu
                        if mv_index < len(self.curves_mv):
                            if is_new_data:
                                self.curves_mv[mv_index].setData(time_data, channel_data)
                            mv_index += 1
            
                            
        except Exception as e:
            self._logger.error(f"Chyba při aktualizaci grafu: {e}")
            # Zastavíme timer při opakovaných chybách
            if hasattr(self, 'plot_error_count'):
                self.plot_error_count += 1
                if self.plot_error_count > 10:
                    self._logger.error("Příliš mnoho chyb při aktualizaci grafu, zastavuji timer")
                    self.gui.plot_timer.stop()
                    self.plot_timer_running = False
            else:
                self.plot_error_count = 1

    def decimation_changed(self):
        self.decimation_factor = self.gui.decimation_value.value()
        self.decimation_mode = self.gui.decimation_mode_dropdown.currentText()
        for curve in self.curves_mv + self.curves_a:
            curve.setDownsampling(self.decimation_factor, auto = self.decimation_factor == 0, method = self.decimation_mode)
        self._logger.info(f"Decimation factor changed to {self.decimation_factor}")
        self._update_plot_data(force_update=True)
    
    def update_time_label(self, current_time, last_update_time):
        current_time = time.localtime(current_time)
        current_time = time.strftime("%H:%M:%S", current_time)
        last_update_time = f"{last_update_time:.3f} s"
        self.gui.time_lable.setText(f"Time: {current_time}    Last update: {last_update_time}")

    def _update_packet_stats(self):
        """Aktualizuje statistiky paketů z připojených zařízení"""
        total_lost = 0
        total_errors = 0
        total_received = 0
        
        for device in self.device_manager._devices.values():
            total_lost += device.lost_packets
            total_errors += device.error_packets
            total_received += device.packet_counter
            
        self.gui.lost_packets_value.setText(str(total_lost))
        self.gui.err_packets_value.setText(str(total_errors))
        self.gui.recv_packets_value.setText(str(f"{total_received} ({total_lost + total_errors + total_received})"))

        self.gui.queued_packets_value.setText(str(self.data_socket.socket.get_buffered_items_count()))
    
    def _reset_counters(self):
        self.device_manager.reset_counters()

    def _on_device_added(self, manager, device):
        """Callback volaný při přidání nového zařízení"""
        def on_id_received(error, old_id, new_id, old_channel_info, new_channel_info):
            if new_channel_info != old_channel_info:
                self._logger.info(f"Device added: {device.addr}, curves initialized")
                self.gui._init_curves_signal.emit()
                # Spustíme timer pro aktualizaci grafu, pokud ještě neběží
                self.gui._start_timer_signal.emit()

        self._logger.debug(f"Requesting device ID {device.addr}")
        device.get_id(on_ack=on_id_received)

    def _select_all_devices(self, checked):
        self._logger.debug("select all")
        for checkbox in self.gui.dev_enable:
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self.gui.select_all_checkbox.blockSignals(True)
        self.gui.select_all_checkbox.setChecked(checked)
        self.gui.select_all_checkbox.blockSignals(False)

    def _invert_selection_devices(self):
        self._logger.debug("invert selection")
        for checkbox in self.gui.dev_enable:
            checkbox.blockSignals(True)
            checkbox.setChecked(not checkbox.isChecked())
            checkbox.blockSignals(False)

        # Po invertu ověř, jestli je všechno zaškrtnuté
        all_checked = all(cb.isChecked() for cb in self.gui.dev_enable)
        self.gui.select_all_checkbox.blockSignals(True)
        self.gui.select_all_checkbox.setChecked(all_checked)
        self.gui.select_all_checkbox.blockSignals(False) 
    
    def _device_selected_changed(self, state):
        # Pokud je něco nezaškrtnuté → "Select All" musí být odškrtnutý
        all_checked = all(cb.isChecked() for cb in self.gui.dev_enable)
        
        self.gui.select_all_checkbox.blockSignals(True)
        self.gui.select_all_checkbox.setChecked(all_checked)
        self.gui.select_all_checkbox.blockSignals(False)

    def add_device(self):
        self._logger.debug("Adding device...")
        if not self.cmd_socket or not self.data_socket:
            self.update_ports()
        for en, addr in zip(self.gui.dev_enable,self.gui.dev_ip_edit):
            if en.isChecked():
                ip, port = addr.text().split(":")
                self.device_manager.add_device((ip, int(port)))
        
        # Po přidání zařízení požádáme o ID pro inicializaci
        #self._logger.debug("Requesting device IDs...")
        #self.device_manager.get_id()
    
    def update_ports(self):
        int(self.gui.command_port_edit.text())
        int(self.gui.data_port_edit.text())
        ip, port = self.gui.dev_ip_edit[0].text().split(":")
        self.cmd_socket.socket.bind((int(self.gui.command_port_edit.text())), use_my_ip = True, device_ip = ip, device_port = int(port))
        self.data_socket.socket.bind((int(self.gui.data_port_edit.text())), use_my_ip = True, device_ip = ip, device_port = int(port))
    
    def start_sampling(self, on_trigger):
        self.num_packets = self.gui.num_packets_spinbox.value()
        if on_trigger:
            self.device_manager.start_on_trigger(self.num_packets)
        else:
            self.device_manager.start_sampling(self.num_packets)



def main(argv):
    with ExitStack() as stack:
        stack.enter_context(logging_:=logger.Logging())
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        app = QApplication(sys.argv)
        app.setFont(QFont("Segoe UI", 9)) 
        plotter = Plotter()
        gui_log_handler = logger.CallbackHandler(sink_text=plotter.gui.log_signal.emit)
        gui_log_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d\t%(levelname)-8s\t%(name)-10s\t%(message)s"))
        gui_log_handler.formatter.datefmt='%H:%M:%S'
        gui_log_handler.setLevel(logging.DEBUG)
        logging_.log_printer.add_handler(gui_log_handler)
        logging_.logger.critical(f"Logging to file: {logging_.log_path}") # This has to be in console, so critical
        plotter.gui.show()
        return app.exec_()

# Spuštění aplikace
if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
    
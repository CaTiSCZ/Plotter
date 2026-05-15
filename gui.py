import pyqtgraph as pg
from PyQt5.QtWidgets import (QLabel, QVBoxLayout, QWidget, QPushButton, QGridLayout, QApplication, QSpinBox, QDoubleSpinBox,
    QCheckBox, QTextEdit, QScrollArea, QLineEdit, QDesktopWidget, QSizePolicy, QComboBox, 
    QMainWindow, QDockWidget, QMenuBar, QMenu, QAction)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont

from device_manager import DeviceManager

"""
TODO:
- ukládání stavu rozložení panelů a jejich nastavení (např. zobrazené křivky, filtry v logu) do konfiguračního souboru (json?)
    - při zavření aplikace a na tlačítko "Save layout" se uloží aktuální rozložení a nastavení panelů do konfiguračního souboru
    - přidat "default layout" do menu, který obnoví výchozí rozložení a nastavení panelů a save as default layout, který uloží aktuální rozložení jako výchozí
- sjednotit seznamy otevřených panelů a jejich počítadel (např. "Graf #1", "Logy #1" atd.) a zajistit, že se správně aktualizují při zavírání panelů
- přidat možnost přejmenovat panely (např. "Graf - Zařízení 1", "Logy - Plotter" atd.)
- opravit přesouvání panelů v rámci okna (ne vždy lze dát panely např. pod sebe atd., špatně se strefuje do místa kde by se to mělo zobrazovat)
- přesunout záložky nahoru a moct přesouvat panel chycením za záložku.

"""


class GraphPanel(QWidget):
    """Samostatný panel pro zobrazení grafu s možností výběru zdroje dat."""
    
    def __init__(self, plotter, data_source=None, parent=None):
        super().__init__(parent)
        self.plotter = plotter
        self.data_source = data_source
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        self.setLayout(layout)
        
        # Nastavení size policy a minimálních rozměrů pro volné přesouvání
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(100, 100)  # Velmi malé minimum pro volnost pohybu
        
        # Výběr zdroje dat
        source_layout = QGridLayout()
        source_layout.addWidget(QLabel("Zdroj dat:"), 0, 0)
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Všechna zařízení", "Zařízení 1", "Zařízení 2", "Zařízení 3"])
        source_layout.addWidget(self.source_combo, 0, 1)
        layout.addLayout(source_layout)
        
        # Graf
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.plot_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.plot_widget.setMinimumSize(50, 50)  # Velmi malé minimum
        self.plot = self.plot_widget.addPlot(title=f"Graf - {self.source_combo.currentText()}")
        self.plot.setLabel('bottom', 'Time', units='s')
        self.plot.setLabel('left', 'Voltage', units='mV')
        self.plot.enableAutoRange(x=True, y=True)
        self.plot.showGrid(x=True, y=True, alpha=0.5)
        self.plot.setMouseEnabled(x=True, y=True)
        layout.addWidget(self.plot_widget)
        
        # Druhá osa Y
        self.right_axis = pg.ViewBox()
        self.plot.showAxis('right')
        self.plot.scene().addItem(self.right_axis)
        self.plot.getAxis('right').linkToView(self.right_axis)
        self.right_axis.setXLink(self.plot)
        self.plot.setLabel('right', 'Current', units='A')
        
        def update_views():
            self.right_axis.setGeometry(self.plot.getViewBox().sceneBoundingRect())
            self.right_axis.linkedViewChanged(self.plot.getViewBox(), self.right_axis.XAxis)
        
        self.plot.getViewBox().sigResized.connect(update_views)
        
        # Update title při změně zdroje
        self.source_combo.currentTextChanged.connect(
            lambda text: self.plot.setTitle(f"Graf - {text}")
        )


class LogPanel(QWidget):
    """Samostatný panel pro zobrazení logů s možností filtrace."""
    
    def __init__(self, plotter, log_level="INFO", log_source=None, parent=None):
        super().__init__(parent)
        self.plotter = plotter
        self.log_level = log_level
        self.log_source = log_source
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        self.setLayout(layout)
        
        # Nastavení size policy a minimálních rozměrů pro volné přesouvání
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(100, 100)  # Velmi malé minimum pro volnost pohybu
        
        # Nastavení filtrace
        filter_layout = QGridLayout()
        filter_layout.addWidget(QLabel("Úroveň logů:"), 0, 0)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.level_combo.setCurrentText(self.log_level)
        filter_layout.addWidget(self.level_combo, 0, 1)
        
        filter_layout.addWidget(QLabel("Zdroj:"), 0, 2)
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Vše", "Plotter", "Device Manager", "Network"])
        filter_layout.addWidget(self.source_combo, 0, 3)
        
        layout.addLayout(filter_layout)
        
        # Log výstup
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet("background-color: #f8f8f8;")
        self.log_output.setFont(QFont("consolas", 9))
        self.log_output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log_output)
        
        # Tlačítka
        button_layout = QGridLayout()
        self.clear_button = QPushButton("Vymazat logy")
        self.clear_button.clicked.connect(self.log_output.clear)
        button_layout.addWidget(self.clear_button, 0, 0)
        
        self.save_button = QPushButton("Uložit logy")
        button_layout.addWidget(self.save_button, 0, 1)
        
        layout.addLayout(button_layout)
    
    def append_log(self, message):
        """Přidá zprávu do logu."""
        self.log_output.append(message)


class Gui(QMainWindow):
    log_signal = pyqtSignal(str)
    _start_timer_signal = pyqtSignal()
    _init_curves_signal = pyqtSignal()

    def __init__(self, plotter):
        super().__init__()

        self.plotter = plotter
        
        # Počítadla otevřených panelů
        self.graph_panel_counter = 0
        self.log_panel_counter = 0
        
        # Seznam otevřených dock widgetů
        self.graph_docks = []
        self.log_docks = []
        
        # Reference na poslední dock pro všechny panely (grafy i logy dohromady)
        self.last_panel_dock = None

        # === Inicializace okna ===
        self.setWindowTitle("UDP Signal Client")
        screen_geometry = QDesktopWidget().availableGeometry()
        width = int(screen_geometry.width() * 0.9)
        height = int(screen_geometry.height() * 0.9)
        self.resize(width, height)
        
        # Povolení záložkového zobrazení dock widgetů
        self.setDockOptions(QMainWindow.AllowTabbedDocks | QMainWindow.AnimatedDocks)
        
        # Nastavení stylu pro záložky - úzké a dynamické
        self.setStyleSheet("""
            QTabBar::tab {
                min-width: 80px;
                max-width: 200px;
                padding: 5px 10px;
            }
            QTabBar {
                qproperty-expanding: false;
            }
        """)
        
        # Vytvoření prázdného centrálního widgetu (minimální velikost)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        central_layout = QVBoxLayout()
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_widget.setLayout(central_layout)
        central_widget.setMaximumHeight(0)  # Skryje centrální widget
        
        # Vytvoření menu baru
        self.create_menu_bar()

                # Vytvoření hlavního panelu jako DockWidget
        main_panel = QWidget()
        main_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.layout = QVBoxLayout()
        main_panel.setLayout(self.layout)
        
        # Obalení hlavního panelu do scroll area
        scroll_area = QScrollArea()
        scroll_area.setWidget(main_panel)
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        self.main_dock = QDockWidget("Hlavní panel", self)
        self.main_dock.setWidget(scroll_area)
        self.main_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.main_dock.setFeatures(QDockWidget.DockWidgetMovable | 
                                     QDockWidget.DockWidgetFloatable | 
                                     QDockWidget.DockWidgetClosable)
        self.main_dock.setMinimumSize(100, 100)  # Velmi malé minimum pro dock
        
        # Přidání hlavního doku
        self.addDockWidget(Qt.TopDockWidgetArea, self.main_dock)
        
        # Reference na poslední dock - začínáme s hlavním panelem
        self.last_panel_dock = self.main_dock
        
# === 1. řádek: GRAF ===
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.plot = self.plot_widget.addPlot(title="Signals from all chanels")
        self.plot.setLabel('bottom', 'Time', units='s')
        self.plot.setLabel('left', 'Voltage', units='mV')
        self.plot.enableAutoRange(x=True, y=True)
        self.plot.showGrid(x=True, y=True, alpha=0.5)
        self.plot.setMouseEnabled(x=True, y=True)
        self.layout.addWidget(self.plot_widget)

# Druhá osa Y
        self.right_axis = pg.ViewBox()
        self.plot.showAxis('right')
        self.plot.scene().addItem(self.right_axis)
        self.plot.getAxis('right').linkToView(self.right_axis)
        self.right_axis.setXLink(self.plot)
        self.plot.setLabel('right', 'Current', units='A')
        
        def update_views():
            self.right_axis.setGeometry(self.plot.getViewBox().sceneBoundingRect())
            self.right_axis.linkedViewChanged(self.plot.getViewBox(), self.right_axis.XAxis)

        self.plot.getViewBox().sigResized.connect(update_views)


# === 2. řádek ===
        row2 = QGridLayout()
# err samples
        self.data_error_label = QLabel("ERR samples\n")
        self.data_error_label.setStyleSheet("font-family: monospace; padding: 6px;")
        self.layout.addWidget(self.data_error_label)
        row2.addWidget(self.data_error_label, 0, 0, 3, 1)
       
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

        self.clear_err_button = QPushButton("Reset counters")
        self.clear_err_button.clicked.connect(plotter._reset_counters)
        row2.addWidget(self.clear_err_button, 0, 3)

# --- Y Min ---
        self.y_min_label = QLabel("Y min:")
        self.y_min_spinbox = QDoubleSpinBox()
        self.y_min_spinbox.setRange(-1000000, 0)
        self.y_min_spinbox.setValue(-33000.0)

        row2.addWidget(self.y_min_label, 0, 4, alignment=Qt.AlignRight)
        row2.addWidget(self.y_min_spinbox, 0, 5, alignment=Qt.AlignLeft)
        self.y_min_spinbox.valueChanged.connect(lambda: plotter._logger.info("TO DO"))
# --- Y Max ---
        self.y_max_label = QLabel("Y max:")
        self.y_max_spinbox = QDoubleSpinBox()
        self.y_max_spinbox.setRange(0, 1000000)
        self.y_max_spinbox.setValue(33000.0)

        row2.addWidget(self.y_max_label, 0, 6, alignment=Qt.AlignRight)
        row2.addWidget(self.y_max_spinbox, 0, 7, alignment=Qt.AlignLeft)
        self.y_max_spinbox.valueChanged.connect(lambda: plotter._logger.info("TO DO"))
# X range
        self.x_range_label = QLabel("X range:")
        self.x_range_spinbox = QDoubleSpinBox()
        self.x_range_spinbox.setRange(0, plotter.buffer_size/DeviceManager.SAMPLES_PER_PACKET)
        self.x_range_spinbox.setValue(200)
        self.x_range_spinbox.setSuffix(" ms")
        row2.addWidget(self.x_range_label, 0, 8, alignment=Qt.AlignRight)
        row2.addWidget(self.x_range_spinbox, 0, 9, alignment=Qt.AlignLeft)
        self.x_range_spinbox.valueChanged.connect(lambda: plotter._logger.info("TO DO"))        

# x auto range        
        self.auto_x_range = True
        self.auto_x_range_checkbox = QCheckBox("Whole buffer")
        self.auto_x_range_checkbox.setChecked(True)
        self.auto_x_range_checkbox.stateChanged.connect(lambda: plotter._logger.info("TO DO"))
        row2.addWidget(self.auto_x_range_checkbox, 0, 10, alignment=Qt.AlignCenter)

# Buffer size
        self.buffer_size_label = QLabel("Buffer size [s]:")
        self.buffer_size_spinbox = QDoubleSpinBox()
        self.buffer_size_spinbox.setRange(0.1, 60.0)
        self.buffer_size_spinbox.setValue(plotter.buffer_length_sec)
   
        row2.addWidget(self.buffer_size_label, 0, 11, alignment=Qt.AlignRight)
        row2.addWidget(self.buffer_size_spinbox, 0, 12, alignment=Qt.AlignLeft)
        self.buffer_size_spinbox.valueChanged.connect(lambda: plotter._logger.info("TO DO"))

        self.decimation_label = QLabel("Decimation:")
        self.decimation_value = QSpinBox()
        self.decimation_value.setRange(0, 10000)
        self.decimation_value.setValue(plotter.decimation_factor)
        row2.addWidget(self.decimation_label, 0, 13, alignment=Qt.AlignRight)
        row2.addWidget(self.decimation_value, 0, 14, alignment=Qt.AlignLeft)
        self.decimation_value.valueChanged.connect(plotter.decimation_changed)

        self.decimation_mode_label = QLabel("Method:")
        self.decimation_mode_dropdown = QComboBox()
        self.decimation_mode_dropdown.addItems(["mean", "peak", "subsample"])
        self.decimation_mode_dropdown.setCurrentText(plotter.decimation_mode)
        row2.addWidget(self.decimation_mode_label, 0, 15, alignment=Qt.AlignRight)
        row2.addWidget(self.decimation_mode_dropdown, 0, 16, alignment=Qt.AlignLeft)
        self.decimation_mode_dropdown.currentTextChanged.connect(plotter.decimation_changed)
# clear graf
        self.clear_button = QPushButton("Clean graf")
        self.clear_button.clicked.connect(lambda: plotter._logger.info("TO DO"))
        row2.addWidget(self.clear_button, 0, 17, alignment=Qt.AlignCenter)
# ------ 3. řádek -----
# Path display (full width)
        self.path_label = QLabel("Path:")
        self.path_display = QLineEdit("C://future_path" + 40 * "/DIR" + "/End")
        self.path_display.setReadOnly(True)
        self.path_display.setStyleSheet("font: consolas; padding: 4px;")
        self.path_display.setFrame(False)
        self.path_display.setCursorPosition(len(self.path_display.text()))
        self.path_display.setAlignment(Qt.AlignLeft)  

        self.path_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
# save buttons
        row2.addWidget(self.path_label, 1, 3, alignment=Qt.AlignRight)
        row2.addWidget(self.path_display, 1, 4, 1, 10)
        
        self.set_path_button = QPushButton("Set path")
        self.set_path_button.clicked.connect(lambda: plotter._logger.info("TO DO"))
        row2.addWidget(self.set_path_button, 2, 4)
        
        self.save_data_button = QPushButton("Save buffer")
        self.save_data_button.clicked.connect(lambda: plotter._logger.info("TO DO"))
        row2.addWidget(self.save_data_button, 2, 5)

        self.AdHoc_safe_button = QPushButton("Ad Hoc save")
        self.AdHoc_safe_button.clicked.connect(lambda: plotter._logger.info("TO DO"))
        row2.addWidget(self.AdHoc_safe_button, 2, 6)

        self.layout.addLayout(row2)
        
# === 3. a další řádky: 4 SLOUPCE ===
        grid = QGridLayout()

# === Sloupec 0: GENERÁTOR & KLIENT IP ===
#ploter ports
        grid.addWidget(QLabel("Plotter ports:"), 0, 0, 1, 5)

        self.listen_all_checkbox = QCheckBox("Listen on all IPs")
        self.listen_all_checkbox.setChecked(True) 
        
        self.cmd_label = QLabel("CMD:")
        self.command_port_edit = QLineEdit(str(plotter.udp_ack_port))
        self.command_port_edit.returnPressed.connect(plotter.update_ports)
        grid.addWidget(self.cmd_label, 1, 0, alignment=Qt.AlignRight)
        grid.addWidget(self.command_port_edit, 1, 1, alignment=Qt.AlignLeft)
        grid.addWidget(self.listen_all_checkbox, 0, 2, 1, 4)

        self.data_label = QLabel("DATA:")
        self.data_port_edit = QLineEdit(str(plotter.udp_data_port))
        self.data_port_edit.returnPressed.connect(plotter.update_ports)
        grid.addWidget(self.data_label, 1,2, alignment=Qt.AlignRight)
        grid.addWidget(self.data_port_edit, 1, 3, alignment=Qt.AlignLeft)


        self.confirm_client_button = QPushButton("Use")
        grid.addWidget(self.confirm_client_button, 2,0, 1,4, alignment=Qt.AlignRight)
        self.confirm_client_button.clicked.connect(plotter.update_ports)


#force trigger
        self.send_trigger_button = QPushButton("Force Trigger")
        self.send_trigger_button.clicked.connect(plotter.device_manager.send_trigger)
        grid.addWidget(self.send_trigger_button, 3,0, 1, 2, alignment=Qt.AlignCenter)
#save on trigger
        self.save_on_trigger = False
        self.save_on_trigger_checkbox = QCheckBox("Save on triger")
        self.save_on_trigger_checkbox.setChecked(True)
        self.save_on_trigger_checkbox.stateChanged.connect(lambda: plotter._logger.info("TO DO"))
        grid.addWidget(self.save_on_trigger_checkbox, 3, 2, 1, 3,  alignment=Qt.AlignLeft)      


#trigger position
        self.trigger_position_label = QLabel("Trigger position:")
        self.trigger_position_spinbox = QSpinBox()
        self.trigger_position_spinbox.setRange(0, int(self.buffer_size_spinbox.value()))
        #self.num_packets_spinbox.setValue(TRIGGER_POSITION)

        grid.addWidget(self.trigger_position_label, 4, 0, 1, 2, alignment=Qt.AlignRight)
        grid.addWidget(self.trigger_position_spinbox, 4, 2, 1, 2)

# === Sloupec 1: ID & Registrace ===

#ping
        self.ping_button = QPushButton("Ping")
        self.ping_button.clicked.connect(plotter.device_manager.ping)
        grid.addWidget(self.ping_button, 1, 5, 1, 2, alignment=Qt.AlignCenter)
# get id
        self.get_id_button = QPushButton("Get ID")
        self.get_id_button.clicked.connect(plotter.device_manager.get_id)
        grid.addWidget(self.get_id_button, 1, 7, 1, 2, alignment=Qt.AlignCenter)
# get receiver
        self.get_receivers_button = QPushButton("Get receivers")
        self.get_receivers_button.clicked.connect(lambda: plotter.device_manager.get_receivers(0))
        grid.addWidget(self.get_receivers_button, 1, 9, 1, 2, alignment=Qt.AlignCenter)
# register receiver
        self.register_text_edit = QLineEdit(f"0.0.0.0:{plotter.udp_data_port}")
        self.register_text_edit.returnPressed.connect(lambda: plotter.device_manager.register_receiver(self.register_text_edit.text(), 0))
        grid.addWidget(QLabel("Register receiver:"), 3, 5, 1, 3)
        grid.addWidget(self.register_text_edit, 4, 5, 1, 2)
        self.register_button = QPushButton("Register")
        self.register_button.clicked.connect(lambda: plotter.device_manager.register_receiver(self.register_text_edit.text(), 0))
        grid.addWidget(self.register_button, 4, 7)
# remove receiver
        self.remove_text_edit = QLineEdit(f"0.0.0.0:{plotter.udp_data_port}")
        self.remove_text_edit.returnPressed.connect(lambda: plotter.device_manager.remove_receiver(self.remove_text_edit.text(), 0))
        grid.addWidget(QLabel("Remove receiver:"), 3, 8, 1, 3)
        grid.addWidget(self.remove_text_edit, 4, 8, 1, 2)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(lambda: plotter.device_manager.remove_receiver(self.remove_text_edit.text(), 0))
        grid.addWidget(self.remove_button, 4, 10)



# === Sloupec 2: Sampling  ===
# num packets
        self.num_packets_label = QLabel("Wanted packets (0 = continue):")
        self.num_packets_spinbox = QSpinBox()
        self.num_packets_spinbox.setRange(0, 100000)
        self.num_packets_spinbox.setValue(plotter.num_packets)

        grid.addWidget(self.num_packets_label, 0, 11)
        grid.addWidget(self.num_packets_spinbox, 0, 12)
# start sampling
        self.start_sampling_button = QPushButton("Start sampling")
        self.start_sampling_button.clicked.connect(lambda: plotter.start_sampling(False))
        grid.addWidget(self.start_sampling_button, 1, 11, 1, 2, alignment=Qt.AlignCenter)
# start on trigger
        self.trigger_sampling_button = QPushButton("Start sampling on trigger")
        self.trigger_sampling_button.clicked.connect(lambda: plotter.start_sampling(True))
        grid.addWidget(self.trigger_sampling_button, 2, 11, 1, 2, alignment=Qt.AlignCenter)
# stop sampling
        self.stop_sampling_button = QPushButton("Stop sampling")
        self.stop_sampling_button.clicked.connect(plotter.device_manager.stop_sampling)
        grid.addWidget(self.stop_sampling_button, 3, 11, 1, 2, alignment=Qt.AlignCenter)

 # cute packets dopsat ester egg
        self.queued_packets = QLabel("Queued packets:")
        self.queued_packets_value = QLabel("0 ; 0")
        grid.addWidget(self.queued_packets, 4, 11, alignment=Qt.AlignCenter)
        grid.addWidget(self.queued_packets_value, 5, 11, alignment=Qt.AlignCenter)

# device adresses
        self.dev_enable = []
        self.dev_ip_edit = []
        device_count = 5
        select_all = True
        for i in range (device_count):
            self.dev_enable.append(QCheckBox(f"Device {i+1}:"))
            self.dev_enable[-1].setChecked(v:=(i in [0]))
            self.dev_enable[-1].stateChanged.connect(plotter._device_selected_changed) 
            grid.addWidget(self.dev_enable[-1], i+1, 13)
            self.dev_ip_edit.append(QLineEdit(f"{plotter.udp_device_addr}:{plotter.udp_device_port}"))
            grid.addWidget(self.dev_ip_edit[-1], i+1, 14, 1, 1)
            select_all &= v

        self.select_all_checkbox = QCheckBox("Select All")
        self.select_all_checkbox.setChecked(select_all)
        grid.addWidget(self.select_all_checkbox, 0, 13) 
        self.select_all_checkbox.stateChanged.connect(lambda state: plotter._select_all_devices(state == Qt.Checked))
        self.invert_selection_button = QPushButton("Invert Selection")
        grid.addWidget(self.invert_selection_button, 0, 14, 1, 1)
        self.invert_selection_button.clicked.connect(plotter._invert_selection_devices)
         
        self.confirm_generator_button = QPushButton("Use")
        grid.addWidget(self.confirm_generator_button, device_count + 1, 13, 1, 1, alignment=Qt.AlignCenter)
        self.confirm_generator_button.clicked.connect(plotter.add_device)

        self.connect_generator_button = QPushButton("Connect")
        grid.addWidget(self.connect_generator_button, device_count + 1, 14,1,1, alignment=Qt.AlignCenter)
        self.connect_generator_button.clicked.connect(lambda: plotter._logger.info("TO DO"))

# === Sloupec 3: LOG ===
        self.log_output = QTextEdit("Log messenge:")
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet("background-color: #f8f8f8;")
        self.log_output.setFont(QFont("consolas", 9)) 
        log_scroll_area = QScrollArea()
        log_scroll_area.setWidgetResizable(True)
        log_scroll_area.setWidget(self.log_output)
        log_scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        grid.addWidget(log_scroll_area, 0, 17, 6, 1) 
        self.log_signal.connect(self.log_output.append)

        self.time_lable = QLabel("Time: 00:00:00 bbLast update: 00:00:00")
        grid.addWidget(self.time_lable, 6, 17, alignment=Qt.AlignCenter) 

        
        grid.setColumnStretch(17, 1)
        for col in range(17):
            grid.setColumnStretch(col, 0)
        

        #===Pevná velikost tlačítek:
        #tlačítka 
        for btn, width  in [
            (self.confirm_client_button, 40),
            (self.send_trigger_button, 110),
            (self.ping_button, 105),
            (self.get_id_button, 105),
            (self.get_receivers_button, 105),
            (self.register_button, 60),
            (self.remove_button, 60),
            (self.start_sampling_button, 200),
            (self.trigger_sampling_button, 200),
            (self.stop_sampling_button, 200),
            (self.confirm_generator_button, 60),
            (self.connect_generator_button, 80),
            
        ]:
            btn.setMinimumWidth(width)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        # labely a checkboxy
        for label in [
            self.listen_all_checkbox,
            self.cmd_label,
            self.data_label,
            self.trigger_position_label,
            self.num_packets_label,
            self.queued_packets,
            self.queued_packets_value,
            self.select_all_checkbox
        ] :
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        # textová pole a spiny
        for widget, width in [
            (self.command_port_edit, 50),
            (self.data_port_edit, 50),
            (self.trigger_position_label, 100),
            (self.register_text_edit, 100),
            (self.remove_text_edit, 100),
            (self.num_packets_spinbox, 50)
        ] + list((k, 100) for k in self.dev_ip_edit):
            widget.setFixedWidth(width)
            widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.layout.addLayout(grid)

        # === Timer pro aktualizaci grafu ===
        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(plotter._update_plot_data)
        # Timer se spustí až po přidání zařízení
        
        self._start_timer_signal.connect(plotter._start_plot_timer)

        # === Registrace událostí ===
        plotter.device_manager.event_device_added.connect(plotter._on_device_added)
        self._init_curves_signal.connect(plotter._init_curves)
    
    def create_menu_bar(self):
        """Vytvoří menu bar s možnostmi pro otevírání panelů."""
        menubar = self.menuBar()
        
        # Menu Panely
        panels_menu = menubar.addMenu("&Panely")
        
        # Akce pro graf
        graph_action = QAction("Nový &graf", self)
        graph_action.setShortcut("Ctrl+G")
        graph_action.setStatusTip("Otevře nový panel s grafem")
        graph_action.triggered.connect(self.open_new_graph_panel)
        panels_menu.addAction(graph_action)
        
        # Akce pro log
        log_action = QAction("Nové &logy", self)
        log_action.setShortcut("Ctrl+L")
        log_action.setStatusTip("Otevře nový panel s logy")
        log_action.triggered.connect(self.open_new_log_panel)
        panels_menu.addAction(log_action)
        
        panels_menu.addSeparator()
        
        # Zavřít všechny panely
        close_all_action = QAction("&Zavřít všechny panely", self)
        close_all_action.triggered.connect(self.close_all_panels)
        panels_menu.addAction(close_all_action)
    
    def open_new_graph_panel(self):
        """Otevře nový dockable panel s grafem."""
        self.graph_panel_counter += 1
        
        # Vytvoření panelu
        graph_panel = GraphPanel(self.plotter)
        
        # Vytvoření dock widgetu
        dock = QDockWidget(f"Graf #{self.graph_panel_counter}", self)
        dock.setWidget(graph_panel)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetMovable | 
                         QDockWidget.DockWidgetFloatable | 
                         QDockWidget.DockWidgetClosable)
        dock.setMinimumSize(100, 100)  # Velmi malé minimum pro volné přesouvání
        
        # Přidání do hlavního okna
        if self.last_panel_dock is not None:
            # Pokud už existuje nějaký panel, přidáme jako záložku k němu
            self.tabifyDockWidget(self.last_panel_dock, dock)
        else:
            # První panel přidáme do dolní oblasti
            self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        
        # Aktivujeme nově přidaný dock (zobrazí se jako aktivní záložka)
        dock.raise_()
        
        # Uložení reference
        self.graph_docks.append(dock)
        self.last_panel_dock = dock
        
        # Při zavření doku jej odebrat ze seznamu
        def remove_dock():
            if dock in self.graph_docks:
                self.graph_docks.remove(dock)
            if self.last_panel_dock == dock:
                # Nastavíme poslední dock na jiný existující panel
                all_docks = self.graph_docks + [d for d, _ in self.log_docks]
                self.last_panel_dock = all_docks[-1] if all_docks else None
        
        dock.destroyed.connect(remove_dock)
        
        self.plotter._logger.debug(f"Otevřen nový panel grafu #{self.graph_panel_counter}")
    
    def open_new_log_panel(self):
        """Otevře nový dockable panel s logy."""
        self.log_panel_counter += 1
        
        # Vytvoření panelu
        log_panel = LogPanel(self.plotter)
        
        # Propojení log signálu s novým panelem
        self.log_signal.connect(log_panel.append_log)
        
        # Vytvoření dock widgetu
        dock = QDockWidget(f"Logy #{self.log_panel_counter}", self)
        dock.setWidget(log_panel)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetMovable | 
                         QDockWidget.DockWidgetFloatable | 
                         QDockWidget.DockWidgetClosable)
        dock.setMinimumSize(100, 100)  # Velmi malé minimum pro volné přesouvání
        
        # Přidání do hlavního okna
        if self.last_panel_dock is not None:
            # Pokud už existuje nějaký panel, přidáme jako záložku k němu
            self.tabifyDockWidget(self.last_panel_dock, dock)
        else:
            # První panel přidáme do dolní oblasti
            self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        
        # Aktivujeme nově přidaný dock (zobrazí se jako aktivní záložka)
        dock.raise_()
        
        # Uložení reference
        self.log_docks.append((dock, log_panel))
        self.last_panel_dock = dock
        
        # Při zavření doku jej odebrat ze seznamu
        def remove_dock():
            for i, (d, p) in enumerate(self.log_docks):
                if d == dock:
                    self.log_signal.disconnect(p.append_log)
                    self.log_docks.pop(i)
                    if self.last_panel_dock == dock:
                        # Nastavíme poslední dock na jiný existující panel
                        all_docks = self.graph_docks + [d for d, _ in self.log_docks]
                        self.last_panel_dock = all_docks[-1] if all_docks else None
                    break
        
        dock.destroyed.connect(remove_dock)
        
        self.plotter._logger.debug(f"Otevřen nový panel logů #{self.log_panel_counter}")
    
    def close_all_panels(self):
        """Zavře všechny otevřené panely."""
        # Zavření všech grafů
        for dock in self.graph_docks[:]:
            dock.close()
        
        # Zavření všech logů
        for dock, _ in self.log_docks[:]:
            dock.close()
        
        self.plotter._logger.debug("Všechny panely byly zavřeny")

    def show(self):
        res = super().show()
        self.plotter.isShown = True
        self.plotter.update_ports()
        return res

    def closeEvent(self, event):
        self.plotter._logger.debug("Ukončuji aplikaci...")

        # Zastavíme timer
        if hasattr(self, 'plot_timer'):
            self.plot_timer.stop()

        self.plotter.data_socket.socket.close()
        self.plotter.cmd_socket.socket.close()
        self.plotter.data_socket.stop()
        self.plotter.cmd_socket.stop()
        self.plotter.device_manager.stop()

        self.plotter.isShown = False
        event.accept()
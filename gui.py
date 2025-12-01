import pyqtgraph as pg
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget, QPushButton, QGridLayout, QApplication, QSpinBox, QDoubleSpinBox, \
    QCheckBox, QTextEdit, QScrollArea, QLineEdit, QDesktopWidget, QSizePolicy 
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont

from device_manager import DeviceManager

class Gui(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, plotter):
        super().__init__()

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
        self.clear_err_button.clicked.connect(lambda: plotter.log_message("TO DO"))
        row2.addWidget(self.clear_err_button, 0, 3)

# --- Y Min ---
        self.y_min_label = QLabel("Y min:")
        self.y_min_spinbox = QDoubleSpinBox()
        self.y_min_spinbox.setRange(-1000000, 0)
        self.y_min_spinbox.setValue(-33000.0)

        row2.addWidget(self.y_min_label, 0, 4, alignment=Qt.AlignRight)
        row2.addWidget(self.y_min_spinbox, 0, 5, alignment=Qt.AlignLeft)
        self.y_min_spinbox.valueChanged.connect(lambda: plotter.log_message("TO DO"))
# --- Y Max ---
        self.y_max_label = QLabel("Y max:")
        self.y_max_spinbox = QDoubleSpinBox()
        self.y_max_spinbox.setRange(0, 1000000)
        self.y_max_spinbox.setValue(33000.0)

        row2.addWidget(self.y_max_label, 0, 6, alignment=Qt.AlignRight)
        row2.addWidget(self.y_max_spinbox, 0, 7, alignment=Qt.AlignLeft)
        self.y_max_spinbox.valueChanged.connect(lambda: plotter.log_message("TO DO"))
# X range
        self.x_range_label = QLabel("X range:")
        self.x_range_spinbox = QDoubleSpinBox()
        self.x_range_spinbox.setRange(0, plotter.buffer_size/DeviceManager.SAMPLES_PER_PACKET)
        self.x_range_spinbox.setValue(200)
        self.x_range_spinbox.setSuffix(" ms")
        row2.addWidget(self.x_range_label, 0, 8, alignment=Qt.AlignRight)
        row2.addWidget(self.x_range_spinbox, 0, 9, alignment=Qt.AlignLeft)
        self.x_range_spinbox.valueChanged.connect(lambda: plotter.log_message("TO DO"))        

# x auto range        
        self.auto_x_range = True
        self.auto_x_range_checkbox = QCheckBox("Whole buffer")
        self.auto_x_range_checkbox.setChecked(True)
        self.auto_x_range_checkbox.stateChanged.connect(lambda: plotter.log_message("TO DO"))
        row2.addWidget(self.auto_x_range_checkbox, 0, 10, alignment=Qt.AlignCenter)

# Buffer size
        self.buffer_size_label = QLabel("Buffer size [s]:")
        self.buffer_size_spinbox = QDoubleSpinBox()
        self.buffer_size_spinbox.setRange(0.1, 60.0)
        self.buffer_size_spinbox.setValue(plotter.buffer_length_sec)
   
        row2.addWidget(self.buffer_size_label, 0, 11, alignment=Qt.AlignRight)
        row2.addWidget(self.buffer_size_spinbox, 0, 12, alignment=Qt.AlignLeft)
        self.buffer_size_spinbox.valueChanged.connect(lambda: plotter.log_message("TO DO"))

# clear graf
        self.clear_button = QPushButton("Clean graf")
        self.clear_button.clicked.connect(lambda: plotter.log_message("TO DO"))
        row2.addWidget(self.clear_button, 0, 13)
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
        self.set_path_button.clicked.connect(lambda: plotter.log_message("TO DO"))
        row2.addWidget(self.set_path_button, 2, 4)
        
        self.save_data_button = QPushButton("Save buffer")
        self.save_data_button.clicked.connect(lambda: plotter.log_message("TO DO"))
        row2.addWidget(self.save_data_button, 2, 5)

        self.AdHoc_safe_button = QPushButton("Ad Hoc save")
        self.AdHoc_safe_button.clicked.connect(lambda: plotter.log_message("TO DO"))
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
        self.save_on_trigger_checkbox.stateChanged.connect(lambda: plotter.log_message("TO DO"))
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
        self.num_packets_spinbox.setRange(0, 10000)
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
        self.connect_generator_button.clicked.connect(lambda: plotter.log_message("TO DO"))

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
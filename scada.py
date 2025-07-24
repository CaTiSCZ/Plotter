# -*- coding: utf-8 -*-
"""Eaton FDDS SCADA – full-featured multi-device UDP client & visualiser
==============================================================================
Jakub Arm

Features:
  - Logging with timestamped pane
  - Enable/disable individual devices
  - Dynamic default IPs and ports based on first device entry
  - Automatic receiver address set from first device
  - Sample count input, Start/StopSampling and Trigger commands
  - Leader/follower selection (none = all followers)
  - Clean Graf functionality
  - Save Data per device to CSV
  - UDP I/O via selector-based asyncio loop (Windows compatible)
"""
from __future__ import annotations
import asyncio, struct, socket, sys, time, threading, csv
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Tuple, List

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QSpinBox, QCheckBox, QTextEdit,
    QScrollArea, QRadioButton, QButtonGroup, QFileDialog
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

# Constants
DEFAULT_CMD_PORT   = 10578
DEFAULT_DATA_PORT  = 10577
RECV_TIMEOUT_S     = 0.3
SAMPLES_PER_PACKET = 200
PACKET_RATE_HZ     = 1000
SAMPLING_PERIOD    = 1/(SAMPLES_PER_PACKET*PACKET_RATE_HZ)
BUFFER_LENGTH_S    = 10
BUFFER_SIZE        = int(BUFFER_LENGTH_S*SAMPLES_PER_PACKET*PACKET_RATE_HZ)

# CRC-16/CCITT checksum
def crc16_ccitt(data: bytes, poly: int=0x1021, crc: int=0xFFFF) -> int:
    for b in data:
        crc ^= b<<8
        for _ in range(8):
            crc = ((crc<<1)^poly)&0xFFFF if crc&0x8000 else (crc<<1)&0xFFFF
    return crc

def _verify_crc(pkt: bytes) -> bytes|None:
    if not pkt or len(pkt)<2: return None
    data, recv_crc = pkt[:-2], struct.unpack('<H', pkt[-2:])[0]
    return data if crc16_ccitt(data)==recv_crc else None

# ID packet parsing from GrafTest fileciteturn4file10
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

# Buffer container
dataclass
class DeviceBuffer:
    def __init__(self, channels:int=3):
        self.lock = threading.Lock()
        self.time   = deque(maxlen=BUFFER_SIZE)
        self.signal = [deque(maxlen=BUFFER_SIZE) for _ in range(channels+1)]
        self.error  = [deque(maxlen=BUFFER_SIZE) for _ in range(channels)]
    def extend(self, t:List[int], samples:List[List[int]], errs:List[int]):
        with self.lock:
            self.time.extend(t)
            for ch, sig in enumerate(samples):
                self.signal[ch+1].extend(sig)
                self.error[ch].extend([errs[ch]]*SAMPLES_PER_PACKET)
            self.signal[0].extend(t)

# Async UDP socket
dataclass
class AsyncSocket:
    def __init__(self, loop, local_port:int, label:str):
        self.loop = loop
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(('0.0.0.0', local_port))
        self.sock,self.queue = sock, asyncio.Queue()
        loop.add_reader(sock.fileno(), self._on_ready)
    def _on_ready(self):
        try: data, addr = self.sock.recvfrom(4096); self.queue.put_nowait((data, addr))
        except: pass
    def send(self, data:bytes, target:Tuple[str,int]): self.sock.sendto(data, target)

# Device client
dataclass
class Device:
    PKT_TYPE_DATA = 2
    def __init__(self, ip:str, cmd_port:int, data_port:int, loop):
        self.ip, self.cmd_port, self.data_port, self.loop = ip,cmd_port,data_port,loop
        self.channels = 3
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(RECV_TIMEOUT_S)
        self.cmd_sock.connect((ip, cmd_port))
        self.buffer = DeviceBuffer(self.channels)
    def _send_cmd(self, code:int, payload:bytes=b'', expect:bool=True):
        pkt = struct.pack('<I', code)+payload; self.cmd_sock.send(pkt)
        if not expect: return None
        try: return self.cmd_sock.recv(2048)
        except socket.timeout: return None
    def ping(self)->bool: return bool(self._send_cmd(0))
    def get_id(self)->dict|None:
        data=_verify_crc(self._send_cmd(1) or b'');
        if not data: return None
        info=parse_id_packet(data); self.channels=info['channels_count']; return info
    def register_receiver(self,addr,port): return self._send_cmd(2, socket.inet_aton(addr)+struct.pack('<H',port))
    def start_sampling(self,n:int=0): return self._send_cmd(5, struct.pack('<I',n))
    def start_sampling_trigger(self,n:int=0): return self._send_cmd(6, struct.pack('<I',n))
    def stop_sampling(self): self._send_cmd(7, expect=False)
    def on_raw_packet(self,pkt:bytes):
        data=_verify_crc(pkt);
        if not data: return
        typ,order=struct.unpack('<HH',data[:4])
        if typ!=self.PKT_TYPE_DATA: return
        off=4; t=[order*SAMPLES_PER_PACKET+k for k in range(SAMPLES_PER_PACKET)]
        samples=[]
        for _ in range(self.channels):
            sig=struct.unpack('<'+'h'*SAMPLES_PER_PACKET, data[off:off+2*SAMPLES_PER_PACKET]); off+=2*SAMPLES_PER_PACKET
            samples.append(list(sig))
        errs=list(data[off:off+self.channels])
        self.loop.call_soon_threadsafe(self.buffer.extend, t, samples, errs)

# Manager
dataclass
class DeviceManager:
    MAX_DEVICES=4
    def __init__(self,data_port:int=DEFAULT_DATA_PORT): self.data_port=data_port; self.devices={}; self.loop=None; self.data_socket=None
    def attach_loop(self,loop): self.loop=loop; self.data_socket=AsyncSocket(loop,self.data_port,'data')
    def clear(self): self.devices.clear()
    def add_device(self,ip,cmd_port=DEFAULT_CMD_PORT):
        if len(self.devices)>=self.MAX_DEVICES or ip in self.devices: return
        self.devices[ip]=Device(ip,cmd_port,self.data_port,self.loop)
    def ping_all(self): return {ip:dev.ping() for ip,dev in self.devices.items()}
    def get_all_ids(self): return {ip:dev.get_id() for ip,dev in self.devices.items()}
    def register_all(self,addr,port):
        for dev in self.devices.values(): dev.register_receiver(addr,port)
    def dispatch_loop(self,signal):
        async def run():
            while True:
                pkt,(ip,_)=await self.data_socket.queue.get()
                if ip in self.devices:
                    self.devices[ip].on_raw_packet(pkt);
                    signal.emit(ip)
        self.loop.create_task(run())

# GUI
class Plotter(QWidget):
    data_ready=pyqtSignal(str)
    def __init__(self,mgr:DeviceManager):
        super().__init__(); self.manager=mgr; self.default_cmd_port=DEFAULT_CMD_PORT
        self.setWindowTitle('Eaton FDDS SCADA'); self.resize(1400,800)
        root=QVBoxLayout(self)
        cfg=QGridLayout(); root.addLayout(cfg)
        # device entries
        self.device_edits=[]; self.device_checks=[]; self.leader_buttons=QButtonGroup(self); self.leader_buttons.setExclusive(True)
        for i in range(DeviceManager.MAX_DEVICES):
            cfg.addWidget(QLabel(f'Device {i+1}'),i,0)
            chk=QCheckBox('Enable'); chk.setChecked(True); cfg.addWidget(chk,i,1); self.device_checks.append(chk)
            le=QLineEdit(); le.setPlaceholderText('ip:port');
            if i==0: le.textChanged.connect(self._update_defaults)
            cfg.addWidget(le,i,2); self.device_edits.append(le)
            rb=QRadioButton('Leader'); cfg.addWidget(rb,i,3); self.leader_buttons.addButton(rb,i)
        cfg.addWidget(QLabel('Receiver addr:port'),0,4)
        self.receiver_edit=QLineEdit(f'0.0.0.0:{DEFAULT_DATA_PORT}'); cfg.addWidget(self.receiver_edit,0,5)
        self.apply_btn=QPushButton('Apply Device List'); cfg.addWidget(self.apply_btn,DeviceManager.MAX_DEVICES,2)
        self.apply_btn.clicked.connect(self._apply_devices)
        # controls
        btns=QHBoxLayout(); root.addLayout(btns)
        for label,fn in [('Ping All',self._ping_all),('Get IDs',self._get_ids),('Register All',self._register_all)]:
            b=QPushButton(label); b.clicked.connect(fn); btns.addWidget(b)
        btns.addWidget(QLabel('Samples:'))
        self.sample_spin=QSpinBox(); self.sample_spin.setRange(0,10000); self.sample_spin.setValue(10); btns.addWidget(self.sample_spin)
        b=QPushButton('Start Sampling'); b.clicked.connect(self._start_sampling); btns.addWidget(b)
        b=QPushButton('Stop Sampling'); b.clicked.connect(self._stop_sampling); btns.addWidget(b)
        b=QPushButton('Clean Graf'); b.clicked.connect(self.clear_plot); btns.addWidget(b)
        b=QPushButton('Save Data'); b.clicked.connect(self.save_data); btns.addWidget(b)
        # plot
        self.plot=pg.GraphicsLayoutWidget(); root.addWidget(self.plot)
        self.ax=self.plot.addPlot(title='Signals'); self.ax.showGrid(x=True,y=True,alpha=0.3)
        self.ax.setLabel('bottom','Time',units='s'); self.ax.setLabel('left','Amplitude')
        self.curves={}
        self.error_lbl=QLabel(); self.error_lbl.setStyleSheet('font-family: monospace'); root.addWidget(self.error_lbl)
        self.log_output=QTextEdit(); self.log_output.setReadOnly(True); self.log_output.setLineWrapMode(QTextEdit.NoWrap)
        self.log_output.setStyleSheet('font-family: monospace; background:#f0f0f0')
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.log_output); root.addWidget(scroll)
        self.timer=QTimer(self); self.timer.setInterval(33); self.timer.timeout.connect(self._update_plot); self.timer.start()
        self.data_ready.connect(lambda ip:None)
    def log_message(self,msg): self.log_output.append(f'[{time.strftime("%H:%M:%S")}] {msg}')
    def _update_defaults(self,text):
        try:
            if not ':' in text: return
            ip,port=text.split(':'); octs=ip.split('.')
            if len(octs)!=4: return
            prefix='.'.join(octs[:3]); base=int(octs[3]); port=self.default_cmd_port
            for i in range(1,DeviceManager.MAX_DEVICES):
                if self.device_checks[i].isChecked(): self.device_edits[i].setText(f'{prefix}.{base+i}:{port}')
            self.device_edits[0].setText(f'{prefix}.{base}:{port}')
            self.receiver_edit.setText(f'{prefix}.1:{DEFAULT_DATA_PORT}')
        except: pass
    def _apply_devices(self):
        self.manager.clear(); self.ax.clear(); count=0
        for chk,le in zip(self.device_checks,self.device_edits):
            if not chk.isChecked(): continue
            txt=le.text().strip();
            if not txt: continue
            try:
                if ':' in txt: ip,p=txt.split(':'); port=int(p)
                else: ip=txt; port=self.default_cmd_port
                self.manager.add_device(ip,port); count+=1
            except: self.log_message(f'Bad entry: {txt}')
        self.log_message(f'Applied {count} devices')
    def _ping_all(self):
        for ip,ok in self.manager.ping_all().items(): self.log_message(f'Ping {ip}: '+('OK' if ok else 'FAIL'))
    def _get_ids(self):
        for ip,info in self.manager.get_all_ids().items():
            self.log_message(f'ID {ip}: '+(f"channels={info['channels_count']}" if info else 'FAIL'))
    def _register_all(self):
        try: addr,pr=self.receiver_edit.text().split(':'); self.manager.register_all(addr,int(pr)); self.log_message(f'Registered {addr}:{pr}')
        except: self.log_message('Bad receiver address')
    def _start_sampling(self):
        n=self.sample_spin.value(); leader_id=self.leader_buttons.checkedId()
        for i,(ip,dev) in enumerate(self.manager.devices.items()):
            if i!=leader_id: dev.start_sampling_trigger(n); self.log_message(f'Trigger on follower {ip}')
        if 0<=leader_id<len(self.manager.devices):
            ip=list(self.manager.devices)[leader_id]; self.manager.devices[ip].start_sampling(n)
            self.log_message(f'Start on leader {ip} (n={n})')
        else:
            for ip,dev in self.manager.devices.items(): dev.start_sampling(n); self.log_message(f'Start on {ip} (n={n})')
    def _stop_sampling(self):
        for ip,dev in self.manager.devices.items(): dev.stop_sampling()
        self.log_message('Stopped all sampling')
    def clear_plot(self):
        for dev in self.manager.devices.values():
            with dev.buffer.lock:
                dev.buffer.time.clear();
                for dq in dev.buffer.signal: dq.clear()
                for dq in dev.buffer.error: dq.clear()
        self.ax.clear(); self.curves={}
        self._update_plot(); self.log_message('Graf cleaned.')
    def save_data(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save Data', '', 'CSV Files (*.csv)')
        if not path: return
        base=path.rstrip('.csv')
        files=[]
        for idx,(ip,dev) in enumerate(self.manager.devices.items()):
            fname=f"{base}_dev{idx}_{ip.replace('.','_')}.csv"
            with open(fname,'w',newline='') as f:
                writer=csv.writer(f)
                header=['time']+ [f'ch{c}' for c in range(dev.channels)]
                writer.writerow(header)
                with dev.buffer.lock:
                    times=list(dev.buffer.time)
                    cols=list(zip(*[list(dev.buffer.signal[c+1]) for c in range(dev.channels)]))
                for t, row in zip(times,cols): writer.writerow([t*SAMPLING_PERIOD,*row])
            files.append(fname)
        self.log_message('Saved data: ' + ', '.join(files))
    def _update_plot(self):
        lines=[]
        for ip,dev in self.manager.devices.items():
            buf=dev.buffer
            if not buf.time: continue
            with buf.lock:
                x=np.array(buf.signal[0])*SAMPLING_PERIOD
                for ch in range(dev.channels):
                    key=(ip,ch)
                    if key not in self.curves:
                        self.curves[key]=self.ax.plot(pen=pg.intColor(hash(key)&0xFFFF,hues=32),name=f'{ip}[{ch}]')
                    y=np.array(buf.signal[ch+1])[-len(x):]
                    self.curves[key].setData(x[-len(y):],y)
                errs=','.join(str(sum(list(buf.error[c])[-SAMPLES_PER_PACKET:])) for c in range(dev.channels))
            lines.append(f'{ip}: {errs}')
        self.error_lbl.setText('Parity errors:\n'+"\n".join(lines))

if __name__=='__main__':
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling,True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,True)
    app=QApplication(sys.argv)
    manager=DeviceManager()
    gui=Plotter(manager)
    def start_loop():
        loop=asyncio.SelectorEventLoop(); asyncio.set_event_loop(loop)
        manager.attach_loop(loop); manager.dispatch_loop(gui.data_ready)
        loop.run_forever()
    threading.Thread(target=start_loop,daemon=True).start()
    gui.show(); sys.exit(app.exec_())
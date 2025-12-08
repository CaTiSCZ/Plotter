import threading
import queue
import time
import socket


class AsyncSocket:
    def __init__(self, socket, max_size = 4096, name = "AsyncSocket"):
        self.socket = socket
        self.max_size = max_size
        self.name = name

        self._on_packet = None
        self._running = True
        

        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True, name=f"{self.name}_listener")
        self._listener_thread.start()

    def _listen_loop(self):
        while self._running:
            try:
                # Kontrola, jestli socket existuje a je svázán
                if not self.socket:
                    time.sleep(0.1)  # Krátké čekání, než bude socket připraven
                    continue
                    
                data, addr = self.socket.recvfrom(self.max_size)
                #print(f"[DEBUG] Příchozí data od {addr}: {data}")
                if self._on_packet is not None:
                    self._on_packet(self, data, addr)
            except socket.timeout:
                #print("timeout")
                continue
            except Exception as e:
                # Ignoruj chyby pokud socket není připraven
                if self._running:
                    time.sleep(0.1)
                continue
    def register(self, callback):
        self._on_packet = callback

    def sendto(self, data: bytes, addr):
        self.socket.sendto(data, addr)

    def stop(self):
        self._running = False
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2)

    def __bool__(self):
        """Vrací True pokud je socket aktivní (běží), jinak False."""
        return bool(self.socket)
 
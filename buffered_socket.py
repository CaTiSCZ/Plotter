import socket
import threading
import queue
import time


class BufferedSocket:
    def __init__(self, max_size = 4096):
        self.max_size = max_size
        self._addr = None
        self._sock = None
        self._sock_lock = threading.Lock()

        self._receive_buffer = queue.Queue()
        self._send_buffer = queue.Queue()

        self._running = False
        self._listener_thread = None
        self._sender_thread = None
        self._timeout = 5.0
        self._received_count = 0

    def bind(self, port: int, use_my_ip: bool = False, device_ip: str = "192.168.1.100", device_port: int = "9999"): 
        self.close()
        with self._sock_lock:
            if use_my_ip:
                try:
                    tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    tmp_sock.connect((device_ip, device_port))  
                    local_ip = tmp_sock.getsockname()[0]
                    tmp_sock.close()
                    print(f"[INFO buffered socket] Detekovaná vlastní IP: {local_ip}")
                except Exception as e:
                    raise RuntimeError(f"Chyba při zjišťování vlastní IP: {e}")
            else:
                local_ip = "0.0.0.0"

            self._addr = (local_ip, port)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(self._addr)
            self._sock.settimeout(5.0)
            self._start()
            #print(f"[INFO] Bound to {self.addr[0]}:{self.addr[1]}")      

    def _start(self):
        if not self._sock:
            raise RuntimeError("Nejdřív zavolej bind() pro nastavení IP a portu.")

        self._running = True
        if self._listener_thread is None or not self._listener_thread.is_alive():
            self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._listener_thread.start()
        if self._sender_thread is None or not self._sender_thread.is_alive():
            self._sender_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._sender_thread.start()

    def close(self):
        self._running = False
        try:
            with self._sock_lock:
                if self._sock:
                    self._sock.close()
                    self._sock = None
        except Exception as e:
            print(f"[CHYBA buffered socket] při zavírání socketu: {e}")
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2)
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=2)



    def _listen_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(self.max_size)
                #print(f"[DEBUG] Příchozí data od {addr}: {data}")
                self._receive_buffer.put((data, addr))
                self._received_count += 1
            except socket.timeout:
                #print("timeout")
                continue
            except (socket.error, OSError) as e:
                if self._running:
                    if isinstance(e, ConnectionResetError):
                        print(f"[VAROVÁNÍ buffered socket] Připojení resetováno hostitelem (pravděpodobně port není aktivní): {e}")
                        continue  # místo break
                    print(f"[CHYBA buffered socket] při příjmu dat: {e}")
                break

    def _send_loop(self):
        
        #last_send = time.time()
        while self._running:
            try:
                data, addr = self._send_buffer.get(timeout=0.1)
                self._sock.sendto(data, addr)
                #now = time.time()
                #print(f"[ODESLÁNO] na {addr} v čase {now:.3f} (interval {now - last_send:.3f}s): {data.decode('utf-8').strip()}")
                #last_send = now
            except queue.Empty:
                continue
            except (socket.error, OSError) as e:
                if self._running:
                    print(f"[CHYBA buffered socket] při odesílání dat: {e}")
                break

    def sendto(self, data: bytes, addr):
        self._send_buffer.put((data, addr))
    
    def settimeout(self, timeout):
        self._timeout = timeout
        
    def recvfrom(self, bufsize):
        try:
            data, addr = self._receive_buffer.get(timeout=self._timeout)
            return data[:bufsize], addr  # <<< zde aplikujeme bufsize limit
        except queue.Empty:
            raise socket.timeout("recvfrom timeout vypršel")

    def get_received_count(self):
        return self._receive_buffer.qsize()


if __name__ == '__main__':

    # Konfigurace adres
    local_host = '127.0.0.1'
    local_port = 5000
    remote_host = '127.0.0.1'
    remote_port = 5001

    relay = BufferedSocket()
    relay.bind(port=local_port, use_my_ip=True, device_ip=remote_host)
    

    print(f"Relay spuštěn. Poslouchám na {local_host}:{local_port}")
    print("Zmáčkni 's' pro zapnutí/vypnutí odesílání \"Ahoj\" každou sekundu.")
    print("Zmáčkni 'q' pro ukončení programu.")

    sending_event = threading.Event()
    quit_event = threading.Event()

    def periodic_sender():
        while not quit_event.is_set():
            if sending_event.is_set():
                message = "Ahoj\n".encode('utf-8')
                relay.sendto(message, (remote_host, remote_port))
            time.sleep(1)

    def input_listener():
        while not quit_event.is_set():
            try:
                cmd = input().strip().lower()
                if cmd == 's':
                    if sending_event.is_set():
                        sending_event.clear()
                        print("[INFO buffered socket] Odesílání VYPNUTO")
                    else:
                        sending_event.set()
                        print(f"[INFO buffered socket] Odesílání ZAPNUTO na {remote_host}:{remote_port}")
                elif cmd == 'q':
                    print("[INFO buffered socket] Ukončuji...")
                    quit_event.set()
                    relay.close()
                    break
            except EOFError:
                quit_event.set()
                relay.close()
                break

    threading.Thread(target=input_listener, daemon=True).start()
    threading.Thread(target=periodic_sender, daemon=True).start()


    try:
        while not quit_event.is_set():
            packet = relay.recvfrom(4096)
            if packet:
                data, addr = packet
                text = data.decode('utf-8', errors='ignore')
                print(f"[PŘIJATO] od {addr}: {text}")
                relay.sendto(data, addr)
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n[INFO] Ukončuji (KeyboardInterrupt)...")
        quit_event.set()
        relay.close()

    except Exception as e:
        print(f"\n[CHYBA]: {e}")
        quit_event.set()
        relay.close()

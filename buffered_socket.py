import socket
import threading
import queue
import time


class UDPRelay:
    def __init__(self):
        self.addr = None
        self.sock = None
        self.sock_lock = threading.Lock()

        self.receive_buffer = queue.Queue()
        self.send_buffer = queue.Queue(maxsize=1000)

        self.running = False
        self.listener_thread = None
        self.sender_thread = None
        self._timeout = 5.0
        self._received_count = 0

    def bind(self, port: int, use_my_ip: bool = False, device_ip: str = "192.168.1.100", device_port: int = "9999"): 
        self.stop()
        with self.sock_lock:
            if use_my_ip:
                try:
                    tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    tmp_sock.connect((device_ip, device_port))  
                    local_ip = tmp_sock.getsockname()[0]
                    tmp_sock.close()
                    print(f"[INFO] Detekovaná vlastní IP: {local_ip}")
                except Exception as e:
                    raise RuntimeError(f"Chyba při zjišťování vlastní IP: {e}")
            else:
                local_ip = "0.0.0.0"

            self.addr = (local_ip, port)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(self.addr)
            self.sock.settimeout(5.0)
            self.start()
            #print(f"[INFO] Bound to {self.addr[0]}:{self.addr[1]}")      

    def start(self):
        if not self.sock:
            raise RuntimeError("Nejdřív zavolej bind() pro nastavení IP a portu.")

        self.running = True
        if self.listener_thread is None or not self.listener_thread.is_alive():
            self.listener_thread = threading.Thread(target=self.listen_loop, daemon=True)
            self.listener_thread.start()
        if self.sender_thread is None or not self.sender_thread.is_alive():
            self.sender_thread = threading.Thread(target=self.send_loop, daemon=True)
            self.sender_thread.start()

    def stop(self):
        self.running = False
        try:
            with self.sock_lock:
                if self.sock:
                    self.sock.close()
                    self.sock = None
        except Exception as e:
            print(f"[CHYBA] při zavírání socketu: {e}")
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2)
        if self.sender_thread and self.sender_thread.is_alive():
            self.sender_thread.join(timeout=2)



    def listen_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                #print(f"[DEBUG] Příchozí data od {addr}: {data}")
                self.receive_buffer.put((data, addr))
                self._received_count += 1
            except socket.timeout:
                #print("timeout")
                continue
            except (socket.error, OSError) as e:
                if self.running:
                    print(f"[CHYBA] při příjmu dat: {e}")
                break

    def send_loop(self):
        
        #last_send = time.time()
        while self.running:
            try:
                data, addr = self.send_buffer.get(timeout=0.1)
                self.sock.sendto(data, addr)
                #now = time.time()
                #print(f"[ODESLÁNO] na {addr} v čase {now:.3f} (interval {now - last_send:.3f}s): {data.decode('utf-8').strip()}")
                #last_send = now
            except queue.Empty:
                continue
            except (socket.error, OSError) as e:
                if self.running:
                    print(f"[CHYBA] při odesílání dat: {e}")
                break

    def sendto(self, data: bytes, addr):
        self.send_buffer.put((data, addr))
    
    def settimeout(self, timeout):
        self._timeout = timeout
        
    def recvfrom(self, bufsize):
        try:
            data, addr = self.receive_buffer.get(timeout=self._timeout)
            return data[:bufsize], addr  # <<< zde aplikujeme bufsize limit
        except queue.Empty:
            raise socket.timeout("recvfrom timeout vypršel")

    def get_received_count(self):
        return self.receive_buffer.qsize()

    def close(self):
        self.stop()

if __name__ == '__main__':

    # Konfigurace adres
    local_host = '127.0.0.1'
    local_port = 5000
    remote_host = '127.0.0.1'
    remote_port = 5001

    relay = UDPRelay()
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
                        print("[INFO] Odesílání VYPNUTO")
                    else:
                        sending_event.set()
                        print(f"[INFO] Odesílání ZAPNUTO na {remote_host}:{remote_port}")
                elif cmd == 'q':
                    print("[INFO] Ukončuji...")
                    quit_event.set()
                    relay.stop()
                    break
            except EOFError:
                quit_event.set()
                relay.stop()
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
        relay.stop()

    except Exception as e:
        print(f"\n[CHYBA]: {e}")
        quit_event.set()
        relay.stop()

import socket
import threading
import queue
import time
import traceback
import logging
import logger


class BufferedSocket:
    def __init__(self, max_size = 4096, name = "BufferedSocket"):
        self._logger = logging.getLogger(__class__.__name__ if logger.application_logger is None else f'{logger.application_logger}.{__class__.__name__}')

        self.max_size = max_size
        self._addr = None
        self._sock = None
        self._sock_lock = threading.Lock()

        self._receive_buffer = queue.Queue()
        self._send_buffer = queue.Queue()

        self._running = False
        self._listener_thread = None
        self._sender_thread = None
        self._timeout = 1
        self._received_count = 0
        self.name = name

    def bind(self, port: int, use_my_ip: bool = False, device_ip: str = "192.168.1.100", device_port: int = 9999): 
        self.close()
        with self._sock_lock:
            if use_my_ip:
                try:
                    tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    tmp_sock.connect((device_ip, device_port))  
                    local_ip = tmp_sock.getsockname()[0]
                    tmp_sock.close()
                    self._logger.info(f"Bind: My IP: {local_ip}")
                except Exception as e:
                    raise RuntimeError(f"Cannot determine my IP: {e}")
            else:
                local_ip = "0.0.0.0"

            self._addr = (local_ip, port)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(self._addr)
            self._sock.settimeout(5.0)
            self._start()
            #print(f"[INFO] Bound to {self.addr[0]}:{self.addr[1]}")     
        return self._addr 

    def _start(self):
        if not self._sock:
            raise RuntimeError("Start: Need to call bind() first to set IP and port.")

        self._running = True
        if self._listener_thread is None or not self._listener_thread.is_alive():
            self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True, name=f"{self.name}_listener")
            self._listener_thread.start()
        if self._sender_thread is None or not self._sender_thread.is_alive():
            self._sender_thread = threading.Thread(target=self._send_loop, daemon=True, name=f"{self.name}_sender")
            self._sender_thread.start()

    def close(self):
        self._running = False
        try:
            with self._sock_lock:
                if self._sock:
                    self._sock.close()
                    self._sock = None
        except Exception as e:
            self._logger.error(f"Error closing socket: {e}")
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2)
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=2)
        self._addr = None

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
                        self._logger.warning(f"Listen loop: {e}")
                        continue  # místo break
                    self._logger.error(f"Listen loop: {e}")
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
                    self._logger.error(f"send loop: {e}")
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
            raise socket.timeout("recvfrom: timeout")

    def get_received_count(self):
        return self._received_count

    def get_buffered_items_count(self):
        return self._receive_buffer.qsize()

    def bound_to(self):
        return self._addr
    
    def __bool__(self):
        """Vrací True pokud je socket aktivní (běží), jinak False."""
        return self._running


def test_main(Socket = BufferedSocket):
    logging_ = logger.Logging()
    # Konfigurace adres
    #local_host = '127.0.0.1'
    local_port = 5000
    remote_host = '127.0.0.1'
    #remote_host = '192.168.1.48'
    remote_port = 5001

    socket_ = Socket()
    bound_to = socket_.bind(port=local_port, use_my_ip=True, device_ip=remote_host)

    print(f"Socket spuštěn. Poslouchám na {bound_to[0]}:{bound_to[1]}")
    print("Zmáčkni 's' pro zapnutí/vypnutí odesílání \"Ahoj\" každou sekundu.")
    print("Zmáčkni 'q' pro ukončení programu.")

    sending_event = threading.Event()
    quit_event = threading.Event()
    sending_event.set()

    def periodic_sender():
        i = 0
        while not quit_event.is_set():
            #print(f"sender period {i} @ {time.time():.3f}")
            i += 1
            if sending_event.is_set():
                message = f"Ahoj {i}\n".encode('utf-8')
                #print("sending... ", end = '')
                socket_.sendto(message, (remote_host, remote_port))
                #print("msg sent")
            time.sleep(1)
        print("[INFO] Ukončuji (periodic_sender)...")

    def input_listener():
        while not quit_event.is_set():
            try:
                packet = socket_.recvfrom(4096)
            except socket.timeout:
                time.sleep(0.01)
            except Exception as e:
                print(f"recvfrom failed with message {e}")
                traceback.print_exc()
            else:
                if packet:
                    data, addr = packet
                    text = data.decode('utf-8', errors='ignore')
                    print(f"[PŘIJATO] od {addr}: {text}")
                    data = text.swapcase().encode('utf-8')
                    socket_.sendto(data, addr)
                else:
                    time.sleep(0.01)
        print("[INFO] Ukončuji (input_listener)...")

    listener = threading.Thread(target=input_listener, daemon=True)
    sender = threading.Thread(target=periodic_sender, daemon=True)
    listener.start()
    sender.start()

    try:
        while not quit_event.is_set():
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
                break
    except KeyboardInterrupt:
        print("\n[INFO] Ukončuji (KeyboardInterrupt)...")
    except Exception as e:
        print(f"\n[CHYBA]: {e}")
    finally:
        quit_event.set()
        listener.join()
        sender.join()
        socket_.close()
        print("[INFO] Vše korektně ukončeno.")

if __name__ == '__main__':
    test_main()

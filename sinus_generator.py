import numpy as np
import threading
import time
import socket

class SinusGenerator:
    def __init__(self, dx=0.1, delay=0.01, ip='127.0.0.1', port=9999):
        self.dx = dx
        self.delay = delay
        self.x = 0
        self.running = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (ip, port)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join()
        self.sock.close()

    def _run(self):
        while self.running:
            y = np.sin(self.x)
            message = f"{self.x} {y}\n".encode('utf-8')
            self.sock.sendto(message, self.target)
            self.x += self.dx
            time.sleep(self.delay)

# --- Samostatné spuštění jako program ---
if __name__ == "__main__":
    gen = SinusGenerator(dx=0.1, delay=0.01, ip='127.0.0.1', port=9999)
    gen.start()
    print("Sinus generátor spuštěn. Ukonči pomocí Ctrl+C.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nUkončuji generátor...")
    finally:
        gen.stop()
        print("Generátor ukončen.")

# sinus_generator.py
import numpy as np
import threading
import time

class SinusGenerator:
    def __init__(self, dx=0.1, delay=0.01):
        self.dx = dx
        self.delay = delay
        self.x = 0
        self.running = False
        self.data = []  # seznam (x, y)
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join()

    def _run(self):
        while self.running:
            y = np.sin(self.x)
            with self._lock:
                self.data.append((self.x, y))
            self.x += self.dx
            time.sleep(self.delay)

    def get_data(self):
        with self._lock:
            return self.data.copy()

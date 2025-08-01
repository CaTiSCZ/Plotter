
import numpy as np

class TripleBuffer:
    def __init__(self, chunk_size):
        self.x = chunk_size
        self.buffer = np.zeros(3 * self.x, dtype=np.float32)
        self.write_pos = self.x  # začínáme od pozice x

    def write_data(self, data: np.ndarray):
        n = len(data)
        end_pos = self.write_pos + n

        if end_pos > 3 * self.x:
            # Před zápisem překračujeme konec => nutno provést kopii a resetovat zápis
            overflow = (self.write_pos + n) - (3 * self.x)
            self._copy_tail()
            self.write_pos = self.x
            end_pos = self.write_pos + n

        self.buffer[self.write_pos:end_pos] = data
        self.write_pos += n

    def _copy_tail(self):
        """Zkopíruje oblast [2x, 3x) do [0, x)."""
        self.buffer[0:self.x] = self.buffer[2 * self.x:3 * self.x]

    def get_display_data(self):
        """Vrací nejnovější x vzorků jako spojitý úsek."""
        start = self.write_pos - self.x
        end = self.write_pos

        if start >= self.x:
            return self.buffer[start:end]
        else:
            # právě proběhl přesun => nejnovější data jsou v [0, x)
            return self.buffer[0:self.x]

#-----
buf = TripleBuffer(chunk_size=1000)

# Zápis 1000 prvků (např. při každé ms)
for i in range(10):
    samples = np.ones(1000) * i
    buf.write_data(samples)
    display = buf.get_display_data()
    print(f"Zobrazeno: {display[:5]} ... {display[-5:]}")
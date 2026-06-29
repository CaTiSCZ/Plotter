"""Performance benchmark: numpy ring buffer vs the old deque storage.

Measures the two hot operations that run on a full buffer:
  * ingest     - extend() of a whole capture (per-packet, runs under buf.lock)
  * plot build - np.array(signal[0]) + argsort + per-channel np.array  (per frame)
  * csv export - list(time)/list(ptp)/list(signal[c]) materialisation

Run:  python tests/bench_ring_buffer.py
"""
import os
import sys
import time
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scada
from scada import NumpyRing, DeviceBuffer, SAMPLES_PER_PACKET, PACKET_RATE_HZ


class RefBuffer:
    def __init__(self, channels, cap):
        self.time = deque(maxlen=cap)
        self.signal = [deque(maxlen=cap) for _ in range(channels + 1)]
        self.error = [deque(maxlen=cap) for _ in range(channels)]
        self.ptp = deque(maxlen=cap)

    def extend(self, t, samples, errs, ptp):
        self.time.extend(t)
        for ch, sig in enumerate(samples):
            self.signal[ch + 1].extend(sig)
            self.error[ch].extend([errs[ch]] * len(sig))
        self.signal[0].extend(t)
        self.ptp.extend(ptp)


def timed(fn, repeat=5):
    best = float('inf')
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def build_packets(n_packets, channels):
    rng = np.random.default_rng(0)
    packets = []
    for p in range(n_packets):
        base = p * SAMPLES_PER_PACKET
        t = list(range(base, base + SAMPLES_PER_PACKET))
        samples = [rng.integers(-30000, 30000, size=SAMPLES_PER_PACKET).tolist()
                   for _ in range(channels)]
        errs = rng.integers(0, 256, size=channels).tolist()
        ptp = [scada.NS_PER_SAMPLE * (base + i) for i in range(SAMPLES_PER_PACKET)]
        packets.append((t, samples, errs, ptp))
    return packets


def plot_build(buf, channels):
    idx = np.array(buf.signal[0], dtype=float)
    order_perm = np.argsort(idx, kind='stable')
    idx = idx[order_perm]
    ys = [np.array(buf.signal[ch + 1], dtype=float)[order_perm] for ch in range(channels)]
    return idx, ys


def csv_build(buf, channels):
    return (list(buf.time), list(buf.ptp),
            [list(buf.signal[c + 1]) for c in range(channels)])


def main():
    seconds = 10
    channels = 3
    n_packets = seconds * PACKET_RATE_HZ
    total_samples = n_packets * SAMPLES_PER_PACKET
    print(f"record: {seconds}s  packets={n_packets}  samples/ch={total_samples}  channels={channels}")
    packets = build_packets(n_packets, channels)

    ref = RefBuffer(channels, scada.BUFFER_SIZE)
    new = DeviceBuffer(channels)

    t_ingest_ref = timed(lambda: [ref.extend(*p) for p in packets], repeat=1)
    t_ingest_new = timed(lambda: [new.extend(*p) for p in packets], repeat=1)
    print(f"ingest  deque={t_ingest_ref*1e3:8.1f} ms   ring={t_ingest_new*1e3:8.1f} ms   "
          f"({t_ingest_ref/t_ingest_new:.2f}x)")

    t_plot_ref = timed(lambda: plot_build(ref, channels))
    t_plot_new = timed(lambda: plot_build(new, channels))
    print(f"plot    deque={t_plot_ref*1e3:8.1f} ms   ring={t_plot_new*1e3:8.1f} ms   "
          f"({t_plot_ref/t_plot_new:.2f}x)", flush=True)

    t_csv_ref = timed(lambda: csv_build(ref, channels), repeat=2)
    t_csv_new = timed(lambda: csv_build(new, channels), repeat=2)
    print(f"csv     deque={t_csv_ref*1e3:8.1f} ms   ring={t_csv_new*1e3:8.1f} ms   "
          f"({t_csv_ref/t_csv_new:.2f}x)", flush=True)

    reserved = scada.BUFFER_SIZE * (8 + 8 + 2 * channels + 2 * channels + 8)
    print(f"ring backing arrays: ~{reserved / 1e6:.1f} MB reserved per device "
          f"(lazy-resident: physical RAM only for samples actually written)", flush=True)


if __name__ == "__main__":
    main()

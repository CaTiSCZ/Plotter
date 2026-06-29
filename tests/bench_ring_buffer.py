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
import csv
import io
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


def csv_old(buf, channels, time_scale, time_zero_s, trim, n_lo, n_hi):
    """The pre-optimisation save_data inner loop: list() the whole buffer, then a
    per-row python filter + per-row writerow."""
    times = list(buf.time)
    ptp = list(buf.ptp)
    signals = [list(buf.signal[c + 1]) for c in range(channels)]
    row_count = min(len(times), len(ptp), *(len(s) for s in signals))
    out = io.StringIO()
    w = csv.writer(out)
    for i in range(row_count):
        if trim and not (n_lo <= times[i] < n_hi):
            continue
        t_shift = times[i] * time_scale - time_zero_s
        w.writerow([t_shift, ptp[i], *(signals[ch][i] for ch in range(channels))])
    return out.getvalue()


def csv_new(buf, channels, time_scale, time_zero_s, trim, n_lo, n_hi):
    """The new vectorised save_data path: numpy mask + deferred tolist of the kept
    rows + a single writerows."""
    times_a = np.asarray(buf.time)
    ptp_a = np.asarray(buf.ptp)
    signals_a = [np.asarray(buf.signal[c + 1]) for c in range(channels)]
    row_count = min(len(times_a), len(ptp_a), *(len(s) for s in signals_a))
    times_a = times_a[:row_count]
    ptp_a = ptp_a[:row_count]
    signals_a = [s[:row_count] for s in signals_a]
    keep = (times_a >= n_lo) & (times_a < n_hi) if trim else np.ones(row_count, dtype=bool)
    t_shift_a = times_a * time_scale - time_zero_s
    out = io.StringIO()
    w = csv.writer(out)
    t_shift_list = t_shift_a[keep].tolist()
    ptp_list = ptp_a[keep].tolist()
    sig_lists = [s[keep].tolist() for s in signals_a]
    w.writerows(zip(t_shift_list, ptp_list, *sig_lists))
    return out.getvalue()


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

    ts = scada.SAMPLING_PERIOD
    # Typical trigger-capture export: trim to a 400-packet window (pre+post).
    pre = post = 200
    tsi = (n_packets // 2) * SAMPLES_PER_PACKET
    n_lo = tsi - pre * SAMPLES_PER_PACKET
    n_hi = tsi + post * SAMPLES_PER_PACKET
    old_win = csv_old(new, channels, ts, tsi * ts, True, n_lo, n_hi)
    new_win = csv_new(new, channels, ts, tsi * ts, True, n_lo, n_hi)
    assert old_win == new_win, "windowed CSV output diverged"
    t_csv_old_win = timed(lambda: csv_old(new, channels, ts, tsi * ts, True, n_lo, n_hi), repeat=3)
    t_csv_new_win = timed(lambda: csv_new(new, channels, ts, tsi * ts, True, n_lo, n_hi), repeat=3)
    print(f"csv window old={t_csv_old_win*1e3:8.1f} ms   new={t_csv_new_win*1e3:8.1f} ms   "
          f"({t_csv_old_win/t_csv_new_win:.2f}x)   [{pre+post} packets kept]", flush=True)

    # Full export (no window trim): worst case, materialise every row.
    old_full = csv_old(new, channels, ts, 0.0, False, 0, 0)
    new_full = csv_new(new, channels, ts, 0.0, False, 0, 0)
    assert old_full == new_full, "full CSV output diverged"
    t_csv_old_full = timed(lambda: csv_old(new, channels, ts, 0.0, False, 0, 0), repeat=1)
    t_csv_new_full = timed(lambda: csv_new(new, channels, ts, 0.0, False, 0, 0), repeat=1)
    print(f"csv full   old={t_csv_old_full*1e3:8.1f} ms   new={t_csv_new_full*1e3:8.1f} ms   "
          f"({t_csv_old_full/t_csv_new_full:.2f}x)", flush=True)

    reserved = scada.BUFFER_SIZE * (8 + 8 + 2 * channels + 2 * channels + 8)
    print(f"ring backing arrays: ~{reserved / 1e6:.1f} MB reserved per device "
          f"(lazy-resident: physical RAM only for samples actually written)", flush=True)


if __name__ == "__main__":
    main()

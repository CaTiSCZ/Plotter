"""Measure the per-frame CPU prep cost of _update_plot's node path (no GUI).

Isolates the work done every frame BEFORE setData: contiguous buffer copy,
float cast, full-buffer stable argsort, fancy-index reorder of every channel,
searchsorted trims. Reports per-device, per-frame milliseconds for a realistic
11 s capture (2.2M samples/channel). Run:  python tests/bench_plot_prep.py
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scada
from scada import SAMPLES_PER_PACKET, PACKET_RATE_HZ, SAMPLING_PERIOD


def make(seconds, channels):
    n = seconds * PACKET_RATE_HZ * SAMPLES_PER_PACKET
    rng = np.random.default_rng(0)
    idx_i64 = np.arange(n, dtype=np.int64)          # buf.signal[0] (sample index)
    sigs = [rng.integers(-30000, 30000, size=n, dtype=np.int16) for _ in range(channels)]
    return n, idx_i64, sigs


def t(fn, *a, repeat=5):
    best = float('inf')
    for _ in range(repeat):
        t0 = time.perf_counter()
        r = fn(*a)
        best = min(best, time.perf_counter() - t0)
    return best, r


def main():
    seconds, channels = 11, 2
    n, idx_i64, sigs = make(seconds, channels)
    tsi = n // 2
    print(f"n={n} samples/ch  channels={channels}", flush=True)

    d_copy, idxf = t(lambda: idx_i64.astype(float))
    print(f"  copy+float cast (signal[0])     {d_copy*1e3:7.1f} ms", flush=True)

    def shift_sort(idxf):
        x = idxf - tsi
        order = np.argsort(x, kind='stable')
        return x[order], order
    d_sort, (xs, order) = t(shift_sort, idxf)
    print(f"  argsort(stable) + reorder idx   {d_sort*1e3:7.1f} ms   <-- full-buffer sort/frame", flush=True)

    d_search, _ = t(lambda: (
        int(np.searchsorted(xs, -3 * SAMPLES_PER_PACKET, side='left')),
        int(np.searchsorted(xs, 5 * SAMPLES_PER_PACKET, side='left'))))
    print(f"  searchsorted x2                 {d_search*1e3:7.1f} ms", flush=True)

    def per_channel():
        out = []
        for s in sigs:
            raw = s.astype(float)[order]
            out.append(raw)
        return out
    d_chan, _ = t(per_channel)
    print(f"  per-channel cast+reorder (x{channels})    {d_chan*1e3:7.1f} ms", flush=True)

    total = d_copy + d_sort + d_search + d_chan
    print(f"  ---- node prep total / device   {total*1e3:7.1f} ms / frame", flush=True)
    print(f"  (x ~3-4 applied devices @ 1 Hz timer)", flush=True)

    # How big is the array handed to pyqtgraph setData?
    print(f"\n  setData receives ~{n} points/curve x {channels} curves/device.", flush=True)
    print(f"  No setDownsampling/setClipToView -> pyqtgraph builds a QPainterPath", flush=True)
    print(f"  over every point and repaints it on the GUI thread.", flush=True)

    # Is argsort even needed? Check if the index is already sorted (in-order capture).
    d_issorted, _ = t(lambda: bool(np.all(np.diff(idxf) >= 0)))
    print(f"\n  np.all(diff>=0) sorted-check    {d_issorted*1e3:7.1f} ms  "
          f"(cheap guard to skip argsort when already ordered)", flush=True)

    # New incremental windowed ordering (plot_sort_order): only the newly appended
    # tail + overlap is (re)checked/sorted, and an already-sorted column returns
    # None so the caller skips the full-buffer reorder entirely.
    from scada import plot_sort_order, PLOT_SORT_MARGIN_SAMPLES
    new_per_frame = PACKET_RATE_HZ * SAMPLES_PER_PACKET // 10   # ~0.1 s of fresh data
    base = idx_i64[: n - new_per_frame]
    state0 = {'n': base.size, 'first': int(base[0]), 'last': int(base[-1]), 'order': None}

    def incr_sorted():
        return plot_sort_order(idx_i64, dict(state0))
    d_incr, (order, _) = t(incr_sorted)
    print(f"\n  plot_sort_order (sorted, +{new_per_frame} new)  "
          f"{d_incr*1e3:7.1f} ms   order={'None (skip reorder)' if order is None else 'array'}", flush=True)
    print(f"    vs full argsort {d_sort*1e3:.1f} ms + reorder {d_chan*1e3:.1f} ms"
          f"  ->  ~{(d_sort + d_chan)/max(d_incr,1e-9):.0f}x less ordering work/frame", flush=True)
    print(f"    (margin = {PLOT_SORT_MARGIN_SAMPLES} samples = {PLOT_SORT_MARGIN_SAMPLES//SAMPLES_PER_PACKET} packets)", flush=True)


if __name__ == "__main__":
    main()

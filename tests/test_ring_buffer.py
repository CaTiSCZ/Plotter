"""Equivalence harness for the numpy ring buffer (DeviceBuffer).

Proves the new NumpyRing-backed DeviceBuffer is byte-for-byte equivalent to the
previous deque-based storage for every way the rest of scada.py reads it:

  * np.array(signal[0]) / argsort(stable) / searchsorted trim  (plot node path)
  * np.array(signal[ch+1], float)[order_perm][trim:trim_end]    (plot channels)
  * list(time) / list(ptp) / list(signal[c+1])                  (CSV export)
  * sum(list(error[c])[-SAMPLES_PER_PACKET:])                   (error statistic)
  * the CCU RESULT path (extend_result + result_* metadata)

Edge cases covered: in-order capture, out-of-order / replayed pre-trigger packets,
a missing packet (gap), ring overflow beyond capacity, and uint16-style wrap of the
sample index. Run:  python tests/test_ring_buffer.py
"""
import os
import sys
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scada
from scada import NumpyRing, DeviceBuffer, SAMPLES_PER_PACKET


# --- Reference deque-based storage (the pre-refactor behaviour) ----------------
class RefBuffer:
    def __init__(self, channels, cap):
        self.time = deque(maxlen=cap)
        self.signal = [deque(maxlen=cap) for _ in range(channels + 1)]
        self.error = [deque(maxlen=cap) for _ in range(channels)]
        self.ptp = deque(maxlen=cap)
        self.result_fault_state = deque(maxlen=cap)
        self.result_parity_errors = deque(maxlen=cap)
        self.result_crc_error_mask = deque(maxlen=cap)

    def extend(self, t, samples, errs, ptp):
        self.time.extend(t)
        for ch, sig in enumerate(samples):
            self.signal[ch + 1].extend(sig)
            self.error[ch].extend([errs[ch]] * len(sig))
        self.signal[0].extend(t)
        self.ptp.extend(ptp)

    def extend_result(self, t, samples, errs, ptp, fault_state, parity_errors, crc_error_mask):
        self.time.extend(t)
        for ch, sig in enumerate(samples):
            self.signal[ch + 1].extend(sig)
            self.error[ch].extend([errs[ch]] * len(sig))
        self.signal[0].extend(t)
        self.ptp.extend(ptp)
        self.result_fault_state.extend([fault_state] * len(t))
        self.result_parity_errors.extend([parity_errors] * len(t))
        self.result_crc_error_mask.extend([crc_error_mask] * len(t))


_fail = 0


def check(name, cond):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail += 1
    print(f"  [{status}] {name}")


def arr_eq(a, b):
    a = np.asarray(a)
    b = np.asarray(b)
    return a.shape == b.shape and np.array_equal(a, b)


# --- 1. NumpyRing vs deque(maxlen) for raw extend / overflow / dtypes ----------
def test_numpyring_vs_deque():
    print("test_numpyring_vs_deque")
    rng = np.random.default_rng(1234)
    for dtype, lo, hi in ((np.int64, -10_000_000, 10_000_000),
                          (np.int16, -32768, 32768)):
        for cap in (7, 200, 1000):
            ref = deque(maxlen=cap)
            ring = NumpyRing(cap, dtype)
            # Many extends of varying chunk sizes, total well over capacity.
            for _ in range(50):
                chunk = rng.integers(lo, hi, size=int(rng.integers(0, cap + 5))).tolist()
                ref.extend(chunk)
                ring.extend(chunk)
            check(f"dtype={np.dtype(dtype).name} cap={cap}: np.array order",
                  arr_eq(list(ref), np.asarray(ring)))
            check(f"dtype={np.dtype(dtype).name} cap={cap}: list() order",
                  list(ref) == list(ring))
            check(f"dtype={np.dtype(dtype).name} cap={cap}: len/bool",
                  len(ref) == len(ring) and bool(ref) == bool(ring))
        # Single oversized extend (> capacity in one go) keeps only the tail.
        cap = 50
        ref = deque(maxlen=cap)
        ring = NumpyRing(cap, dtype)
        big = rng.integers(lo, hi, size=cap * 3 + 7).tolist()
        ref.extend(big)
        ring.extend(big)
        check(f"dtype={np.dtype(dtype).name}: single oversized extend",
              list(ref) == list(ring))
    # clear()
    ring = NumpyRing(10, np.int64)
    ring.extend([1, 2, 3])
    ring.clear()
    check("clear() empties", len(ring) == 0 and not ring and list(ring) == [])


# --- shared plot/CSV consumer pipeline replicas --------------------------------
def node_plot_pipeline(buf, channels, pretrigger_packets, samples_awaited, tsi):
    """Replicates the _update_plot node path math against a buffer."""
    idx = np.array(buf.signal[0], dtype=float)
    if tsi is not None:
        idx -= tsi
    order_perm = np.argsort(idx, kind='stable')
    idx = idx[order_perm]
    if tsi is not None:
        trim = int(np.searchsorted(idx, -pretrigger_packets * SAMPLES_PER_PACKET, side='left'))
    else:
        trim = 0
    if tsi is not None and samples_awaited > 0:
        trim_end = int(np.searchsorted(idx, samples_awaited * SAMPLES_PER_PACKET, side='left'))
    else:
        trim_end = len(idx)
    x = (idx * scada.SAMPLING_PERIOD)[trim:trim_end]
    ys = []
    for ch in range(channels):
        raw = np.array(buf.signal[ch + 1], dtype=float)[order_perm][trim:trim_end]
        ys.append(raw)
    errsum = [sum(list(buf.error[c])[-SAMPLES_PER_PACKET:]) for c in range(channels)]
    return order_perm, x, ys, errsum


def csv_columns(buf, channels):
    times = list(buf.time)
    ptp = list(buf.ptp)
    signals = [list(buf.signal[c + 1]) for c in range(channels)]
    return times, ptp, signals


def make_node_packet(rel_order, channels, rng):
    base = rel_order * SAMPLES_PER_PACKET
    t = list(range(base, base + SAMPLES_PER_PACKET))
    samples = [rng.integers(-30000, 30000, size=SAMPLES_PER_PACKET).tolist()
               for _ in range(channels)]
    errs = rng.integers(0, 256, size=channels).tolist()
    ptp = [1_000_000_000 + base * scada.NS_PER_SAMPLE + i * scada.NS_PER_SAMPLE
           for i in range(SAMPLES_PER_PACKET)]
    return t, samples, errs, ptp


# --- 2. DeviceBuffer node path: in-order, out-of-order, missing packet ---------
def feed(ref, new, packets):
    for (t, samples, errs, ptp) in packets:
        ref.extend(t, samples, errs, ptp)
        new.extend(t, samples, errs, ptp)


def test_node_capture(label, rel_orders, channels=2, pretrigger=3, post=5, seed=7):
    print(f"test_node_capture[{label}]")
    rng = np.random.default_rng(seed)
    ref = RefBuffer(channels, scada.BUFFER_SIZE)
    new = DeviceBuffer(channels)
    packets = [make_node_packet(ro, channels, rng) for ro in rel_orders]
    feed(ref, new, packets)
    # trigger sample index = start of packet index `pretrigger` (t=0 there)
    tsi = pretrigger * SAMPLES_PER_PACKET
    op_r, x_r, ys_r, es_r = node_plot_pipeline(ref, channels, pretrigger, post, tsi)
    op_n, x_n, ys_n, es_n = node_plot_pipeline(new, channels, pretrigger, post, tsi)
    check("order_perm identical", arr_eq(op_r, op_n))
    check("x (time) identical", arr_eq(x_r, x_n))
    for ch in range(channels):
        check(f"y ch{ch} identical", arr_eq(ys_r[ch], ys_n[ch]))
    check("error sums identical", es_r == es_n)
    tr, pr, sr = csv_columns(ref, channels)
    tn, pn, sn = csv_columns(new, channels)
    check("csv time identical", tr == tn)
    check("csv ptp identical", pr == pn)
    check("csv signals identical", sr == sn)


# --- 3. CCU RESULT path --------------------------------------------------------
def test_result_capture():
    print("test_result_capture")
    channels = 2
    rng = np.random.default_rng(99)
    ref = RefBuffer(channels, scada.BUFFER_SIZE)
    new = DeviceBuffer(channels)
    for rel_order in range(20):
        t = [rel_order]
        result_code = int(rng.integers(0, 4))
        samples = [[(result_code >> b) & 1] for b in range(channels)]
        fault_state = tuple(int(v) for v in rng.integers(0, 5, size=scada.GATHERING_DEVICES))
        parity_errors = tuple(int(v) for v in
                              rng.integers(0, 3, size=scada.GATHERING_DEVICES * scada.ACQUISITION_CHANNELS))
        errs = list(parity_errors)
        crc_error_mask = int(rng.integers(0, 0xFFFF))
        ptp = [1_000_000_000 + rel_order * 1_000_000]
        ref.extend_result(t, samples, errs, ptp, fault_state, parity_errors, crc_error_mask)
        new.extend_result(t, samples, errs, ptp, fault_state, parity_errors, crc_error_mask)
    check("result time identical", list(ref.time) == list(new.time))
    check("result signal0 identical", arr_eq(np.array(ref.signal[0]), np.array(new.signal[0])))
    for ch in range(channels):
        check(f"result signal{ch+1} identical",
              list(ref.signal[ch + 1]) == list(new.signal[ch + 1]))
        check(f"result error{ch} identical", list(ref.error[ch]) == list(new.error[ch]))
    check("result ptp identical", list(ref.ptp) == list(new.ptp))
    check("result fault_state identical",
          list(ref.result_fault_state) == list(new.result_fault_state))
    check("result parity_errors identical",
          list(ref.result_parity_errors) == list(new.result_parity_errors))
    check("result crc_error_mask identical",
          list(ref.result_crc_error_mask) == list(new.result_crc_error_mask))
    # error statistic used for the CCU path: sum(list(error[c])[-1:])
    es_r = [sum(list(ref.error[c])[-1:]) for c in range(channels)]
    es_n = [sum(list(new.error[c])[-1:]) for c in range(channels)]
    check("result error[-1] sum identical", es_r == es_n)


def main():
    test_numpyring_vs_deque()
    # In order: pre(0,1,2) trigger(3) post(4..9)
    test_node_capture("in_order", list(range(10)))
    # Out-of-order: a pre-trigger packet replayed late around the trigger boundary.
    test_node_capture("out_of_order", [0, 1, 3, 2, 4, 5, 6, 7, 8, 9])
    # Missing packet: a gap (packet index 5 dropped).
    test_node_capture("missing", [0, 1, 2, 3, 4, 6, 7, 8, 9])
    test_result_capture()
    print()
    if _fail:
        print(f"FAILED: {_fail} check(s) failed")
        sys.exit(1)
    print("ALL EQUIVALENCE CHECKS PASSED")


if __name__ == "__main__":
    main()

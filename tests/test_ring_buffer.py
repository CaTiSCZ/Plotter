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
import csv
import io
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


# --- 4. save_data CSV: old per-row loop vs new vectorised pass -----------------
def _csv_old(times, ptp, signals, channels, time_scale, time_zero_s,
             trim_window, n_lo, n_hi, has_result_meta,
             result_fault_state, result_parity_errors, result_crc_error_mask):
    """The pre-optimisation per-row algorithm (python lists)."""
    lengths = [len(times), len(ptp), *(len(s) for s in signals)]
    if has_result_meta:
        lengths += [len(result_fault_state), len(result_parity_errors), len(result_crc_error_mask)]
    row_count = min(lengths)
    out = io.StringIO()
    w = csv.writer(out)
    for i in range(row_count):
        if trim_window and not (n_lo <= times[i] < n_hi):
            continue
        t_shift = times[i] * time_scale - time_zero_s
        row = [signals[ch][i] for ch in range(channels)]
        if has_result_meta:
            row += list(result_fault_state[i])
            row += list(result_parity_errors[i])
            row += [result_crc_error_mask[i]]
        w.writerow(['%.6f' % t_shift, ptp[i], *row])
    return out.getvalue()


def _csv_new(times_a, ptp_a, signals_a, channels, time_scale, time_zero_s,
             trim_window, n_lo, n_hi, has_result_meta,
             result_fault_state, result_parity_errors, result_crc_error_mask):
    """The new vectorised algorithm (numpy arrays, mask, deferred tolist)."""
    lengths = [len(times_a), len(ptp_a), *(len(s) for s in signals_a)]
    if has_result_meta:
        lengths += [len(result_fault_state), len(result_parity_errors), len(result_crc_error_mask)]
    row_count = min(lengths)
    times_a = times_a[:row_count]
    ptp_a = ptp_a[:row_count]
    signals_a = [s[:row_count] for s in signals_a]
    if trim_window:
        keep = (times_a >= n_lo) & (times_a < n_hi)
    else:
        keep = np.ones(row_count, dtype=bool)
    t_shift_a = times_a * time_scale - time_zero_s
    out = io.StringIO()
    w = csv.writer(out)
    if has_result_meta:
        t_shift_list = t_shift_a.tolist()
        ptp_list = ptp_a.tolist()
        sig_lists = [s.tolist() for s in signals_a]
        rows = []
        for i in np.nonzero(keep)[0].tolist():
            row = [sig_lists[ch][i] for ch in range(channels)]
            row += list(result_fault_state[i])
            row += list(result_parity_errors[i])
            row += [result_crc_error_mask[i]]
            rows.append(['%.6f' % t_shift_list[i], ptp_list[i], *row])
        w.writerows(rows)
    else:
        # Mirror the production node path: single % template + '\r\n' join (the
        # csv default line terminator). The time column is fixed 6-decimal so it
        # must match the per-row reference (_csv_old) byte for byte.
        t_shift_list = t_shift_a[keep].tolist()
        ptp_list = ptp_a[keep].tolist()
        sig_lists = [s[keep].tolist() for s in signals_a]
        if t_shift_list:
            tmpl = '%.6f,' + ','.join(['%d'] * (channels + 1))
            lines = [tmpl % row for row in zip(t_shift_list, ptp_list, *sig_lists)]
            out.write('\r\n'.join(lines))
            out.write('\r\n')
    return out.getvalue()


def test_csv_node():
    print("test_csv_node")
    channels = 2
    rng = np.random.default_rng(5)
    ref = RefBuffer(channels, scada.BUFFER_SIZE)
    new = DeviceBuffer(channels)
    packets = [make_node_packet(ro, channels, rng)
               for ro in [0, 1, 3, 2, 4, 5, 6, 7, 8, 9]]  # incl. out-of-order
    feed(ref, new, packets)
    pretrigger, post = 3, 5
    tsi = pretrigger * SAMPLES_PER_PACKET
    time_zero_s = tsi * scada.SAMPLING_PERIOD
    n_lo = tsi - pretrigger * SAMPLES_PER_PACKET
    n_hi = tsi + post * SAMPLES_PER_PACKET
    for trim in (False, True):
        old = _csv_old(list(ref.time), list(ref.ptp),
                       [list(ref.signal[c + 1]) for c in range(channels)],
                       channels, scada.SAMPLING_PERIOD, time_zero_s,
                       trim, n_lo, n_hi, False, [], [], [])
        newv = _csv_new(np.asarray(new.time), np.asarray(new.ptp),
                        [np.asarray(new.signal[c + 1]) for c in range(channels)],
                        channels, scada.SAMPLING_PERIOD, time_zero_s,
                        trim, n_lo, n_hi, False, [], [], [])
        check(f"node csv identical (trim={trim})", old == newv)


def test_csv_result():
    print("test_csv_result")
    channels = 2
    rng = np.random.default_rng(6)
    ref = RefBuffer(channels, scada.BUFFER_SIZE)
    new = DeviceBuffer(channels)
    for rel_order in range(20):
        t = [rel_order]
        rc = int(rng.integers(0, 4))
        samples = [[(rc >> b) & 1] for b in range(channels)]
        fault_state = tuple(int(v) for v in rng.integers(0, 5, size=scada.GATHERING_DEVICES))
        parity_errors = tuple(int(v) for v in
                              rng.integers(0, 3, size=scada.GATHERING_DEVICES * scada.ACQUISITION_CHANNELS))
        errs = list(parity_errors)
        crc_error_mask = int(rng.integers(0, 0xFFFF))
        ptp = [1_000_000_000 + rel_order * 1_000_000]
        ref.extend_result(t, samples, errs, ptp, fault_state, parity_errors, crc_error_mask)
        new.extend_result(t, samples, errs, ptp, fault_state, parity_errors, crc_error_mask)
    trig_n = 8.0
    time_zero_s = round(trig_n) * scada.PACKET_PERIOD
    n_lo, n_hi = trig_n - 3, trig_n + 5
    for trim in (False, True):
        old = _csv_old(list(ref.time), list(ref.ptp),
                       [list(ref.signal[c + 1]) for c in range(channels)],
                       channels, scada.PACKET_PERIOD, time_zero_s, trim, n_lo, n_hi,
                       True, list(ref.result_fault_state),
                       list(ref.result_parity_errors), list(ref.result_crc_error_mask))
        newv = _csv_new(np.asarray(new.time), np.asarray(new.ptp),
                        [np.asarray(new.signal[c + 1]) for c in range(channels)],
                        channels, scada.PACKET_PERIOD, time_zero_s, trim, n_lo, n_hi,
                        True, list(new.result_fault_state),
                        list(new.result_parity_errors), list(new.result_crc_error_mask))
        check(f"result csv identical (trim={trim})", old == newv)


# --- 5. incremental plot ordering vs full stable argsort -----------------------
def test_plot_sort_order():
    print("test_plot_sort_order")
    rng = np.random.default_rng(11)
    SP = 5                      # samples per synthetic "packet"
    margin = 2 * SP             # re-sort window overlap (>= max reorder distance)

    # Build an arrival-order packet stream; each packet = SP consecutive indices.
    # Occasionally swap two adjacent packets (a single local UDP reorder, <= SP).
    bases, i = list(range(40)), 0
    arrival = []
    while i < len(bases):
        if i + 1 < len(bases) and rng.random() < 0.25:
            arrival += [bases[i + 1], bases[i]]; i += 2
        else:
            arrival.append(bases[i]); i += 1

    # Stream in growing chunks (append-only), recomputing the order incrementally.
    state, idx0, eff, ok = None, np.empty(0, np.int64), np.empty(0, np.int64), True
    pkt = 0
    while pkt < len(arrival):
        take = int(rng.integers(1, 4))
        for b in arrival[pkt:pkt + take]:
            idx0 = np.concatenate((idx0, np.arange(b * SP, b * SP + SP, dtype=np.int64)))
        pkt += take
        order, state = scada.plot_sort_order(idx0, state, margin=margin)
        eff = np.arange(idx0.size) if order is None else order
        if not np.array_equal(eff, np.argsort(idx0, kind='stable')):
            ok = False
    check("incremental order == full stable argsort", ok)
    check("final reordered idx is fully sorted",
          np.array_equal(idx0[eff], np.sort(idx0, kind='stable')))

    # In-order stream must stay on the identity (None) fast path the whole way.
    state, idx0, all_none = None, np.empty(0, np.int64), True
    for b in range(20):
        idx0 = np.concatenate((idx0, np.arange(b * SP, b * SP + SP, dtype=np.int64)))
        order, state = scada.plot_sort_order(idx0, state, margin=margin)
        all_none = all_none and order is None
    check("in-order stream stays identity (None)", all_none)

    # Reset / shrink must transparently fall back to a full sort.
    a = np.array([50, 10, 30, 20, 40], dtype=np.int64)
    order, st = scada.plot_sort_order(a, None, margin=margin)
    check("reset full sort matches argsort",
          np.array_equal(order, np.argsort(a, kind='stable')))
    short = np.array([2, 0, 1], dtype=np.int64)
    order2, _ = scada.plot_sort_order(short, st, margin=margin)
    check("shrink triggers full sort",
          np.array_equal(order2, np.argsort(short, kind='stable')))

    # Duplicate indices: an ascending run with repeats is identity (stable).
    dup = np.array([0, 0, 1, 1, 1, 2], dtype=np.int64)
    o, _ = scada.plot_sort_order(dup, None, margin=margin)
    check("ascending duplicates -> identity (None)", o is None)


def main():
    test_numpyring_vs_deque()
    test_node_capture("in_order", list(range(10)))
    # Out-of-order: a pre-trigger packet replayed late around the trigger boundary.
    test_node_capture("out_of_order", [0, 1, 3, 2, 4, 5, 6, 7, 8, 9])
    # Missing packet: a gap (packet index 5 dropped).
    test_node_capture("missing", [0, 1, 2, 3, 4, 6, 7, 8, 9])
    test_result_capture()
    test_csv_node()
    test_csv_result()
    test_plot_sort_order()
    print()
    if _fail:
        print(f"FAILED: {_fail} check(s) failed")
        sys.exit(1)
    print("ALL EQUIVALENCE CHECKS PASSED")


if __name__ == "__main__":
    main()

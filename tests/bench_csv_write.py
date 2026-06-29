"""Find the fastest CSV formatting for save_data's full export (~2.2M rows/device).

numpy (np.char.mod / np.savetxt) proved slower than csv.writer and changes the
float text. This isolates the *formatting* cost (build the text in memory, no disk)
across pure-python strategies, and reports formatting differences vs the current
csv.writer output. Run:  python tests/bench_csv_write.py
"""
import os
import sys
import io
import csv
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scada
from scada import SAMPLES_PER_PACKET, PACKET_RATE_HZ

SP = scada.SAMPLING_PERIOD


def make_data(seconds, channels):
    n = seconds * PACKET_RATE_HZ * SAMPLES_PER_PACKET
    rng = np.random.default_rng(0)
    idx = np.arange(n, dtype=np.int64)
    tsi = n // 2
    t_shift = (idx - tsi).astype(np.float64) * SP
    ptp = idx.astype(np.int64) * scada.NS_PER_SAMPLE
    signals = [rng.integers(-30000, 30000, size=n, dtype=np.int16) for _ in range(channels)]
    return t_shift, ptp, signals


def fmt_current(t_shift, ptp, signals, channels):
    """Current path: csv.writer over zip(tolist...)."""
    out = io.StringIO()
    w = csv.writer(out)
    t_list = t_shift.tolist()
    ptp_list = ptp.tolist()
    sig_lists = [s.tolist() for s in signals]
    w.writerows(zip(t_list, ptp_list, *sig_lists))
    return out.getvalue()


def fmt_join_pct(t_shift, ptp, signals, channels):
    """'\\n'.join of '%r,%d,...' per row (str(float) time, no csv module)."""
    cols = [t_shift.tolist(), ptp.tolist(), *[s.tolist() for s in signals]]
    tmpl = '%r,' + ','.join(['%d'] * (channels + 1))
    return '\n'.join([tmpl % row for row in zip(*cols)]) + '\n'


def fmt_join_tstr(t_shift, ptp, signals, channels):
    """Pre-format the int columns to str arrays via map(str), join with the
    str(float) time list. Builds rows with str.join."""
    t_list = [repr(x) for x in t_shift.tolist()]
    int_cols = [list(map(str, ptp.tolist()))] + [list(map(str, s.tolist())) for s in signals]
    rows = zip(t_list, *int_cols)
    return '\n'.join([','.join(r) for r in rows]) + '\n'


def fmt_join_fixed(t_shift, ptp, signals, channels, tdec=6):
    """Fixed-decimal time ('%.6f', exact for the 5 us / 1 ms grid) + int columns,
    all formatted per row with a single % template -> one '\\n'.join."""
    cols = [t_shift.tolist(), ptp.tolist(), *[s.tolist() for s in signals]]
    tmpl = f'%.{tdec}f,' + ','.join(['%d'] * (channels + 1))
    return '\n'.join([tmpl % row for row in zip(*cols)]) + '\n'


def count_diffs(a, b):
    diffs = total = 0
    sample = []
    for la, lb in zip(a.splitlines(), b.splitlines()):
        for x, y in zip(la.split(','), lb.split(',')):
            total += 1
            if x != y:
                diffs += 1
                if len(sample) < 4:
                    sample.append((x, y))
    return diffs, total, sample


def timed(fn, *args, repeat=1):
    best = float('inf')
    res = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        res = fn(*args)
        best = min(best, time.perf_counter() - t0)
    return best, res


def main():
    seconds = 11
    channels = 3
    print(f"record={seconds}s channels={channels} samples/ch={seconds*PACKET_RATE_HZ*SAMPLES_PER_PACKET}", flush=True)
    t_shift, ptp, signals = make_data(seconds, channels)

    dt_cur, s_cur = timed(fmt_current, t_shift, ptp, signals, channels)
    print(f"current   csv.writer(zip(tolist))   {dt_cur*1e3:8.1f} ms", flush=True)

    for name, fn in (("join %r", fmt_join_pct),
                     ("join str", fmt_join_tstr),
                     ("join %.6f", fmt_join_fixed)):
        dt, s = timed(fn, t_shift, ptp, signals, channels)
        d, tot, sample = count_diffs(s_cur, s)
        note = "identical" if d == 0 else f"{d}/{tot} differ e.g. {sample[:2]}"
        print(f"{name:10s}{'':22s}{dt*1e3:8.1f} ms   ({dt_cur/dt:.2f}x)   {note}", flush=True)

    # Real disk write incl. fsync: csv.writer vs the production join path, byte-compare.
    import tempfile
    tmp = tempfile.gettempdir()
    p_old = os.path.join(tmp, 'bench_old.csv')
    p_new = os.path.join(tmp, 'bench_new.csv')

    def write_old(path):
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['time', 'ptp_ns'] + [f'ch{c}' for c in range(channels)])
            w.writerows(zip(t_shift.tolist(), ptp.tolist(), *[s.tolist() for s in signals]))
            f.flush(); os.fsync(f.fileno())

    def write_new(path):
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['time', 'ptp_ns'] + [f'ch{c}' for c in range(channels)])
            tmpl = '%r,' + ','.join(['%d'] * (channels + 1))
            lines = [tmpl % row for row in zip(t_shift.tolist(), ptp.tolist(), *[s.tolist() for s in signals])]
            f.write('\r\n'.join(lines)); f.write('\r\n')
            f.flush(); os.fsync(f.fileno())

    t0 = time.perf_counter(); write_old(p_old); dt_o = time.perf_counter() - t0
    t0 = time.perf_counter(); write_new(p_new); dt_n = time.perf_counter() - t0
    with open(p_old, 'rb') as a, open(p_new, 'rb') as b:
        identical = a.read() == b.read()
    print(f"\nDISK incl. fsync ({os.path.getsize(p_old)/1e6:.0f} MB/file):", flush=True)
    print(f"  csv.writer  {dt_o*1e3:8.1f} ms", flush=True)
    print(f"  join %r     {dt_n*1e3:8.1f} ms   ({dt_o/dt_n:.2f}x)   bytes {'IDENTICAL' if identical else 'DIFFER'}", flush=True)
    for p in (p_old, p_new):
        try:
            os.remove(p)
        except OSError:
            pass


if __name__ == "__main__":
    main()

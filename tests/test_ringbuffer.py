"""Unit tests for RingBuffer out-of-order and wrap-around handling."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

# Minimal stubs so scada.py can be imported for RingBuffer
import types
logger_mod = types.ModuleType('logger')
logger_mod.application_logger = None
sys.modules['logger'] = logger_mod

# We only need the RingBuffer class; import it from scada
from scada import RingBuffer


def test_basic_insert():
    """Sequential insert should fill correctly."""
    rb = RingBuffer(channels=2, capacity_packets=10, samples_per_packet=4)
    samples = [[1, 2, 3, 4], [5, 6, 7, 8]]
    errors = [0, 1]
    assert rb.is_empty
    assert rb.insert(0, samples, errors)
    assert not rb.is_empty
    snap = rb.snapshot()
    assert snap['total_packets'] == 1
    assert snap['total_samples'] == 4
    np.testing.assert_array_equal(snap['data'][0], [1, 2, 3, 4])
    np.testing.assert_array_equal(snap['data'][1], [5, 6, 7, 8])
    np.testing.assert_array_equal(snap['errors'][0], [0])
    np.testing.assert_array_equal(snap['errors'][1], [1])
    print("PASS: test_basic_insert")


def test_out_of_order():
    """Insert packets 0, 2, 1 — all should appear in correct positions."""
    rb = RingBuffer(channels=1, capacity_packets=10, samples_per_packet=2)
    rb.insert(0, [[10, 11]], [0])
    rb.insert(2, [[30, 31]], [0])
    rb.insert(1, [[20, 21]], [0])
    snap = rb.snapshot()
    assert snap['total_packets'] == 3
    assert snap['count'] == 3
    np.testing.assert_array_equal(snap['data'][0], [10, 11, 20, 21, 30, 31])
    np.testing.assert_array_equal(snap['filled'], [True, True, True])
    print("PASS: test_out_of_order")


def test_gap_tracking():
    """Insert packets 0 and 5 — gap of 4 packets shown as unfilled."""
    rb = RingBuffer(channels=1, capacity_packets=10, samples_per_packet=1)
    rb.insert(0, [[100]], [0])
    rb.insert(5, [[500]], [0])
    snap = rb.snapshot()
    assert snap['total_packets'] == 6
    assert snap['count'] == 2
    assert snap['filled'][0] == True
    assert snap['filled'][5] == True
    assert not snap['filled'][1]
    assert not snap['filled'][2]
    print("PASS: test_gap_tracking")


def test_wrap_around_uint16():
    """Insert near 0xFFFF then wrap to 0x0000."""
    rb = RingBuffer(channels=1, capacity_packets=100, samples_per_packet=1)
    rb.insert(0xFFFE, [[1]], [0])
    rb.insert(0xFFFF, [[2]], [0])
    rb.insert(0x0000, [[3]], [0])
    rb.insert(0x0001, [[4]], [0])
    snap = rb.snapshot()
    assert snap['total_packets'] == 4
    assert snap['count'] == 4
    np.testing.assert_array_equal(snap['data'][0], [1, 2, 3, 4])
    print("PASS: test_wrap_around_uint16")


def test_eviction():
    """When buffer is full, old data is evicted."""
    cap = 5
    rb = RingBuffer(channels=1, capacity_packets=cap, samples_per_packet=1)
    for i in range(cap + 3):
        rb.insert(i, [[i * 10]], [0])
    snap = rb.snapshot()
    # Only last `cap` packets should remain
    assert snap['total_packets'] == cap
    assert snap['count'] == cap
    # Data should be packets 3,4,5,6,7 (values 30,40,50,60,70)
    np.testing.assert_array_equal(snap['data'][0], [30, 40, 50, 60, 70])
    print("PASS: test_eviction")


def test_discard_old():
    """Packet older than write_base is discarded."""
    rb = RingBuffer(channels=1, capacity_packets=10, samples_per_packet=1)
    rb.insert(100, [[1]], [0])
    rb.insert(105, [[2]], [0])
    # Packet 99 is behind write_base=100
    assert not rb.insert(99, [[9]], [0])
    snap = rb.snapshot()
    assert snap['count'] == 2
    print("PASS: test_discard_old")


def test_large_ooo_burst():
    """Simulate realistic OOO burst: packets arriving with local reordering."""
    import random
    rb = RingBuffer(channels=2, capacity_packets=100, samples_per_packet=4)
    # First packet must be the lowest (sets write_base), then shuffle the rest
    orders = list(range(20))
    random.seed(42)
    shuffled_tail = orders[1:]
    random.shuffle(shuffled_tail)
    orders = [0] + shuffled_tail
    for o in orders:
        ch0 = [o * 4 + k for k in range(4)]
        ch1 = [-(o * 4 + k) for k in range(4)]
        rb.insert(o, [ch0, ch1], [0, 0])
    snap = rb.snapshot()
    assert snap['total_packets'] == 20
    assert snap['count'] == 20
    assert all(snap['filled'])
    # Verify data is in correct order regardless of insertion order
    expected_ch0 = np.arange(80, dtype=np.int16)
    np.testing.assert_array_equal(snap['data'][0], expected_ch0)
    print("PASS: test_large_ooo_burst")


def test_ccu_single_sample():
    """CCU device: 1 sample per packet."""
    rb = RingBuffer(channels=4, capacity_packets=50, samples_per_packet=1)
    for i in range(10):
        samples = [[i & 1], [(i >> 1) & 1], [(i >> 2) & 1], [(i >> 3) & 1]]
        rb.insert(i, samples, [0, 0, 0, 0])
    snap = rb.snapshot()
    assert snap['total_packets'] == 10
    assert snap['total_samples'] == 10
    assert snap['count'] == 10
    print("PASS: test_ccu_single_sample")


def test_snapshot_empty():
    """Snapshot on empty buffer returns None."""
    rb = RingBuffer(channels=2, capacity_packets=10, samples_per_packet=4)
    assert rb.snapshot() is None
    print("PASS: test_snapshot_empty")


def test_clear():
    """Clear resets buffer to empty."""
    rb = RingBuffer(channels=1, capacity_packets=10, samples_per_packet=1)
    rb.insert(5, [[42]], [1])
    rb.clear()
    assert rb.is_empty
    assert rb.snapshot() is None
    print("PASS: test_clear")


if __name__ == '__main__':
    test_basic_insert()
    test_out_of_order()
    test_gap_tracking()
    test_wrap_around_uint16()
    test_eviction()
    test_discard_old()
    test_large_ooo_burst()
    test_ccu_single_sample()
    test_snapshot_empty()
    test_clear()
    print("\nAll RingBuffer tests passed!")

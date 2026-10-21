"""Timestamp-based ISO capture and exact plot clipping."""
import csv
import os
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import scada
from PyQt5.QtWidgets import QApplication


TRIGGER_NS = 1_800_000_000_000_000_000


def iso_packet(order, offset_ms, resistances=None):
    timestamp = TRIGGER_NS + int(offset_ms * 1_000_000)
    values = [float(offset_ms)] * 6 + (
        [float(offset_ms)] * 4 if resistances is None else list(resistances))
    body = scada.ISO_RESULT_PREFIX_STRUCT.pack(
        scada.Device.PKT_TYPE_ISO_RESULT, order,
        timestamp // 1_000_000_000, timestamp % 1_000_000_000,
        *values, 0, 0)
    return body + struct.pack('<H', scada.crc16_ccitt(body))


class IsoCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_ptp = vars(scada.ptp_mode).copy()
        self.manager = scada.DeviceManager()
        loop = Mock()
        loop.call_soon_threadsafe.side_effect = lambda callback, *args: callback(*args)
        self.device = scada.Device('127.0.0.6', 10578, 10579, loop, self.manager)
        self.device._set_isomon_stream_config(
            scada.ISOMON_STREAM_ENABLE_ISO_RESULT, 1, 200000)
        self.manager.devices[self.device.ip] = self.device
        scada.ptp_mode.enabled = True
        scada.ptp_mode.trigger_mode = True
        scada.ptp_mode.immediate_trigger = False
        scada.ptp_mode.waiting_for_trigger = False
        scada.ptp_mode.device_manager = self.manager
        scada.ptp_mode.pretrigger_ms = 1000
        scada.ptp_mode.posttrigger_ms = 3000
        self.plotter = None

    def tearDown(self):
        if self.plotter is not None:
            self.plotter.close()
        self.device.cmd_sock.close()
        self.manager.cmd_sock.close()
        vars(scada.ptp_mode).clear()
        vars(scada.ptp_mode).update(self.old_ptp)
        self.app.processEvents()

    def trigger(self, history):
        for order, offset in enumerate(history):
            self.device.on_raw_packet(iso_packet(order, offset))
        self.manager.ptp_trigger(100, trigger_ptp_ns=TRIGGER_NS)

    def test_irregular_capture_keeps_bracketing_packets_and_waits_for_end(self):
        self.trigger([-9000, -3500, -1000, -400])
        self.assertEqual(list(self.device.buffer.iso_ptp),
                         [TRIGGER_NS + ms * 1_000_000 for ms in (-3500, -1000, -400)])
        for order, offset in enumerate((0, 100, 500, 1400, 3000), start=4):
            self.device.on_raw_packet(iso_packet(order, offset))
            self.assertTrue(self.device.capture_active)
        self.device.on_raw_packet(iso_packet(9, 12750))
        self.assertFalse(self.device.capture_active)
        self.device.on_raw_packet(iso_packet(10, 17000))
        self.assertEqual(list(self.device.buffer.iso_ptp)[-1], TRIGGER_NS + 12_750_000_000)
        self.assertEqual(self.device.capture_counters[scada.STREAM_ISO_RESULT], 9)

    def test_no_iso_history_still_uses_actual_trigger_time(self):
        self.trigger([])
        self.device.on_raw_packet(iso_packet(20, 1200))
        self.assertTrue(self.device.capture_active)
        self.device.on_raw_packet(iso_packet(21, 7000))
        self.assertFalse(self.device.capture_active)
        x, _ = self.device.iso_capture_rows(np.asarray(self.device.buffer.iso_ptp))
        np.testing.assert_allclose(x, [1.2, 7.0])

    def test_zero_window_requires_packets_strictly_outside_both_bounds(self):
        scada.ptp_mode.pretrigger_ms = 0
        scada.ptp_mode.posttrigger_ms = 0
        self.trigger([-5000, -500, 0])
        self.assertEqual(len(self.device.buffer.iso_ptp), 2)
        self.assertTrue(self.device.capture_active)
        self.device.on_raw_packet(iso_packet(3, 4000))
        self.assertFalse(self.device.capture_active)

    def test_dense_packets_do_not_finish_capture_early(self):
        self.trigger([-2000])
        for order in range(1, 40):
            self.device.on_raw_packet(iso_packet(order, order * 50))
        self.assertTrue(self.device.capture_active)
        self.device.on_raw_packet(iso_packet(40, 3001))
        self.assertFalse(self.device.capture_active)
        self.assertEqual(self.device.capture_counters[scada.STREAM_ISO_RESULT], 41)

    def test_corrupt_or_duplicate_packet_cannot_finish_capture(self):
        self.trigger([-2000])
        self.device.on_raw_packet(iso_packet(1, 500))
        self.device.on_raw_packet(iso_packet(1, 7000))
        corrupt = bytearray(iso_packet(2, 7000))
        corrupt[-1] ^= 1
        self.device.on_raw_packet(bytes(corrupt))
        self.assertTrue(self.device.capture_active)
        self.device.on_raw_packet(iso_packet(2, 7000))
        self.assertFalse(self.device.capture_active)

    def test_hardware_trigger_passes_ptp_time_not_latest_iso_time(self):
        self.device.on_raw_packet(iso_packet(0, -2000))
        scada.ptp_mode.waiting_for_trigger = True
        seconds, ns = divmod(TRIGGER_NS, 1_000_000_000)
        packet = scada.TRIGGER_PACKET_STRUCT.pack(
            self.device.PKT_TYPE_TRIGGER, 100, 0, seconds, ns)
        self.device.on_raw_packet(packet)
        self.assertEqual(self.device.iso_trigger_ptp_ns, TRIGGER_NS)
        self.assertEqual(self.device.iso_capture_window_ns,
                         (TRIGGER_NS - 1_000_000_000, TRIGGER_NS + 3_000_000_000))

    def test_immediate_iso_trigger_uses_current_packet_timestamp(self):
        self.device.on_raw_packet(iso_packet(0, -2000))
        scada.ptp_mode.waiting_for_trigger = True
        scada.ptp_mode.immediate_trigger = True
        self.device.on_raw_packet(iso_packet(1, 0))
        self.assertEqual(self.device.iso_trigger_ptp_ns, TRIGGER_NS)
        self.assertTrue(self.device.capture_active)
        self.assertEqual(len(self.device.buffer.iso_ptp), 2)

    def test_immediate_wide_trigger_aligns_iso_to_first_aggregated_record(self):
        self.device.stream_enable_mask |= scada.ISOMON_STREAM_ENABLE_WIDE
        self.device.on_raw_packet(iso_packet(0, -2000))
        scada.ptp_mode.waiting_for_trigger = True
        scada.ptp_mode.immediate_trigger = True
        agg_count, records = 10, 20
        last_ns = TRIGGER_NS + (records - 1) * agg_count * scada.NS_PER_SAMPLE
        seconds, ns = divmod(last_ns, 1_000_000_000)
        body = scada.STRUCT.WIDE_HEADER.pack(
            self.device.PKT_TYPE_WIDE, 100, seconds, ns,
            agg_count, records, scada.WIDE_CH_POS, 0)
        body += struct.pack(f'<{records}i', *([100] * records))
        body += b'\0' * 8
        self.device.on_raw_packet(body + struct.pack('<H', scada.crc16_ccitt(body)))
        self.assertEqual(self.device.iso_trigger_ptp_ns, TRIGGER_NS)
        self.assertEqual(list(self.device.buffer.ptp)[0], TRIGGER_NS)
        self.assertEqual(self.device.trigger_sample_index(scada.STREAM_WIDE), 0)

    def test_wide_completion_does_not_end_iso_capture_or_clear_indicator(self):
        self.device.stream_enable_mask |= scada.ISOMON_STREAM_ENABLE_WIDE
        self.trigger([-2000])
        self.device.capture_counters[scada.STREAM_WIDE] = self.device.capture_limits[scada.STREAM_WIDE]
        self.assertFalse(self.device._capture_all_streams_done())
        with patch.object(scada, 'USE_OPENGL', False):
            self.plotter = scada.Plotter(self.manager)
        self.plotter._sampling_start_pending = False
        self.plotter._recompute_expected_stream_packets(1000, 3000)
        self.plotter._set_sampling_indicator(self.plotter.start_sampling_btn)
        received = {key: max(1, value) for key, value in self.plotter.expected_by_stream.items()}
        self.plotter._clear_sampling_indicator_if_complete(received)
        self.assertIsNotNone(self.plotter.sampling_indicator_button)
        self.device.on_raw_packet(iso_packet(1, 12000))
        self.assertFalse(self.device.capture_active)
        self.plotter._clear_sampling_indicator_if_complete(received)
        self.assertIsNone(self.plotter.sampling_indicator_button)

    def test_pretrigger_readiness_uses_history_duration_not_packet_count(self):
        with patch.object(scada, 'USE_OPENGL', False):
            self.plotter = scada.Plotter(self.manager)
        self.device.on_raw_packet(iso_packet(0, -2000))
        self.device.on_raw_packet(iso_packet(1, -1999))
        self.assertFalse(self.plotter._ptp_pretrigger_ready(1000))
        self.device.on_raw_packet(iso_packet(2, 0))
        self.assertTrue(self.plotter._ptp_pretrigger_ready(1000))
        self.plotter._recompute_expected_stream_packets(1000, 3000)
        self.assertEqual(self.plotter.expected_by_stream[(self.device.ip, scada.STREAM_ISO_RESULT)], 0)

    def test_clipping_never_extrapolates_missing_history(self):
        x, y = scada._clip_curve_window(np.array([1.2, 7.0]), np.array([12., 70.]), -1, 3)
        np.testing.assert_allclose(x, [1.2, 3])
        np.testing.assert_allclose(y, [12, 30])

    def test_zero_pretrigger_three_second_plot_with_final_packet_at_twelve_seconds(self):
        scada.ptp_mode.pretrigger_ms = 0
        self.trigger([-9000, -3500, -400])
        self.device.on_raw_packet(iso_packet(3, 1400))
        self.device.on_raw_packet(iso_packet(4, 12750))
        self.assertEqual(len(self.device.buffer.iso_ptp), 3)
        with patch.object(scada, 'USE_OPENGL', False):
            self.plotter = scada.Plotter(self.manager)
        self.plotter.device_edits[scada.ISOMON_DEVICE_INDEX].setText('127.0.0.6:10578')
        self.plotter._update_plot(force=True)
        x, y = self.plotter.curves[(self.device.ip, scada.ISO_U_FIELDS[0])].getData()
        np.testing.assert_allclose(x, [0, 1.4, 3])
        np.testing.assert_allclose(y, [0, 1400, 3000], atol=0.001)

    def test_plot_interpolates_but_csv_preserves_real_boundary_packets(self):
        self.trigger([-9000, -3500, -400])
        self.device.on_raw_packet(iso_packet(3, 1400))
        self.device.on_raw_packet(iso_packet(4, 12750))
        with patch.object(scada, 'USE_OPENGL', False):
            self.plotter = scada.Plotter(self.manager)
        self.plotter.device_edits[scada.ISOMON_DEVICE_INDEX].setText('127.0.0.6:10578')
        self.plotter._recompute_expected_stream_packets(1000, 3000)
        self.plotter._update_plot(force=True)
        for name in scada.ISO_U_FIELDS:
            x, y = self.plotter.curves[(self.device.ip, name)].getData()
            np.testing.assert_allclose(x, [-1, -0.4, 1.4, 3])
            np.testing.assert_allclose(y, [-1000, -400, 1400, 3000], atol=0.001)
        for curve in self.plotter.ax_iso_r_curves.values():
            x, _ = curve.getData()
            self.assertEqual((x[0], x[-1]), (-1, 3))
        self.assertIn('/?', self.plotter.error_lbl.text())
        with tempfile.TemporaryDirectory() as directory:
            paths = self.plotter.save_data(os.path.join(directory, 'capture'))
            iso_path = next(path for path in paths if path.endswith('_iso_result.csv'))
            with open(iso_path, newline='') as source:
                rows = list(csv.DictReader(source))
        self.assertEqual([float(row['time']) for row in rows], [-3.5, -0.4, 1.4, 12.75])
        self.assertEqual(int(rows[0]['ptp_ns']), TRIGGER_NS - 3_500_000_000)

    def test_nonfinite_resistances_survive_capture_plot_and_csv(self):
        self.trigger([-2000])
        values = (float('nan'), float('inf'), -float('inf'), -123.5)
        self.device.on_raw_packet(iso_packet(1, 0))
        self.device.on_raw_packet(iso_packet(2, 1000, values))
        self.device.on_raw_packet(iso_packet(3, 2000))
        self.device.on_raw_packet(iso_packet(4, 4000))
        with patch.object(scada, 'USE_OPENGL', False), \
             patch.object(scada, 'ISOMON_ENABLED', True):
            self.plotter = scada.Plotter(self.manager)
            self.plotter.device_edits[scada.ISOMON_DEVICE_INDEX].setText('127.0.0.6:10578')
            self.plotter._update_plot(force=True)
        for name, value in zip(scada.ISO_R_FIELDS, values):
            curve = self.plotter.ax_iso_r_curves[name]
            x, y = curve.getData()
            self.assertEqual(curve.opts['connect'], 'finite')
            actual = y[np.flatnonzero(x == 1.0)[0]]
            if np.isnan(value):
                self.assertTrue(np.isnan(actual))
            else:
                self.assertEqual(actual, value)
            path = curve.curve.getPath()
            self.assertGreater(path.elementCount(), 0)
            for index in range(path.elementCount()):
                element = path.elementAt(index)
                self.assertTrue(np.isfinite(element.x) and np.isfinite(element.y))
                if index and element.isLineTo() and not np.isfinite(value):
                    previous = path.elementAt(index - 1)
                    self.assertFalse(previous.x < 1.0 < element.x)
        self.assertIn('NaN', self.plotter._format_iso_value('r1_via_r3', values[0], True))
        self.assertIn('+Inf', self.plotter._format_iso_value('r1_via_r3', values[1], True))
        self.assertIn('-Inf', self.plotter._format_iso_value('r1_via_r3', values[2], True))
        self.assertIn('--', self.plotter._format_iso_value('r1_via_r3', values[0], False))
        with tempfile.TemporaryDirectory() as directory:
            paths = self.plotter.save_data(os.path.join(directory, 'nonfinite'))
            iso_path = next(path for path in paths if path.endswith('_iso_result.csv'))
            with open(iso_path, newline='') as source:
                rows = list(csv.DictReader(source))
        row = next(row for row in rows if float(row['time']) == 1.0)
        self.assertEqual([row[name] for name in scada.ISO_R_FIELDS],
                         ['nan', 'inf', '-inf', '-123.5'])
        self.assertTrue(np.isnan(float(row[scada.ISO_R_FIELDS[0]])))
        self.assertTrue(np.isposinf(float(row[scada.ISO_R_FIELDS[1]])))
        self.assertTrue(np.isneginf(float(row[scada.ISO_R_FIELDS[2]])))
        curve = next(iter(self.plotter.ax_iso_r_curves.values()))
        curve.setData([0, 1, 2], [float('nan'), float('inf'), -float('inf')])
        self.assertEqual(curve.dataBounds(1), (None, None))
        self.assertEqual(curve.curve.getPath().elementCount(), 0)
        self.plotter.ax_iso_r.autoRange()


if __name__ == '__main__':
    unittest.main()

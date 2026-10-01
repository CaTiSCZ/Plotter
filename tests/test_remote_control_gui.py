"""Offscreen integration test for the HTTP-to-Qt GUI bridge."""
import json
import base64
import os
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import scada
from PyQt5.QtWidgets import QApplication, QPushButton, QLabel
from remote_control import RemoteControlServer


class RemoteControlGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = scada.DeviceManager()
        self.plotter = scada.Plotter(self.manager)
        self.plotter.remote_control_enabled.blockSignals(True)
        self.plotter.remote_control_enabled.setChecked(True)
        self.plotter.remote_control_enabled.blockSignals(False)
        self.plotter.remote_control_addr.setEnabled(False)
        self.server = RemoteControlServer(self.plotter._remote_bridge.dispatch)
        address = self.server.start('127.0.0.1:0')
        host, port = address.rsplit(':', 1)
        self.base_url = f'http://{host}:{port}'

    def tearDown(self):
        self.server.stop()
        self.plotter._remote_stop()
        self.plotter.close()
        self.manager.cmd_sock.close()
        self.app.processEvents()

    def _request_on_gui_event_loop(self, path, *, method='GET', body=None):
        result = {}
        finished = threading.Event()

        def request():
            try:
                data = json.dumps(body).encode('utf-8') if body is not None else None
                headers = {'Content-Type': 'application/json'} if body is not None else {}
                response = urlopen(Request(self.base_url + path, data=data, headers=headers, method=method), timeout=3)
                result['status'] = response.status
                result['payload'] = json.loads(response.read())
            except Exception as exc:
                result['error'] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=request)
        thread.start()
        deadline = time.monotonic() + 4
        while not finished.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        thread.join(timeout=1)
        self.assertTrue(finished.is_set(), 'HTTP request did not complete')
        if 'error' in result:
            raise result['error']
        return result['status'], result['payload']

    def test_http_snapshot_and_widget_update_run_through_qt(self):
        status, snapshot = self._request_on_gui_event_loop('/api/v1/ui/state')
        self.assertEqual(status, 200)
        widgets = {widget['id']: widget for widget in snapshot['data']['widgets']}
        self.assertIn('remote_control_enabled', widgets)
        self.assertIn('remote_control_addr_port', widgets)
        self.assertIn('remote_control_token', widgets)
        self.assertTrue(widgets['remote_control_token']['configured'] is False)
        self.assertFalse(widgets['remote_control_addr_port']['enabled'])
        self.assertIn('statistics', widgets)
        self.assertIn('log_output', widgets)
        self.assertGreater(len(widgets), 25)

        status, updated = self._request_on_gui_event_loop(
            '/api/v1/widgets/posttrigger_ms', method='POST',
            body={'action': 'set_value', 'value': 123})
        self.assertEqual(status, 200)
        self.assertEqual(updated['data']['value'], 123)
        self.assertEqual(self.plotter.sample_spin.value(), 123)
        status, token = self._request_on_gui_event_loop(
            '/api/v1/widgets/remote_control_token', method='POST',
            body={'action': 'set_text', 'text': 'api-token'})
        self.assertEqual(status, 200)
        self.assertEqual(token['data']['text'], '********')
        self.assertEqual(self.plotter._remote_server._token, 'api-token')

    def test_remote_control_stays_beside_live_values(self):
        config_grid = self.plotter.layout().itemAt(0).layout()
        isomon_values = config_grid.itemAtPosition(scada.ISOMON_DEVICE_INDEX, 7).widget()
        isomon_values.setMinimumWidth(1100)
        self.plotter.resize(3800, 800)
        self.plotter.show()
        self.app.processEvents()

        for column in (0, 1, 3):
            self.assertLess(config_grid.cellRect(0, column).width(), 150)
        self.assertLess(self.plotter.device_edits[0].x(), 500)
        remote_label = config_grid.itemAtPosition(0, 8).widget()
        self.assertLess(self.plotter.width() - self.plotter.remote_control_addr.geometry().right(), 40)
        self.assertGreaterEqual(remote_label.x(), isomon_values.x() + 800)
        device_address = self.plotter.device_edits[0]
        self.assertGreaterEqual(device_address.width(), 200)
        for field in (self.plotter.remote_control_addr, self.plotter.remote_control_token,
                      self.plotter.receiver_edit, self.plotter.measurement_number_edit):
            self.assertEqual(field.width(), device_address.width())
        self.assertEqual(config_grid.getItemPosition(config_grid.indexOf(isomon_values))[3], 3)

    def test_isomon_live_values_fit_fhd_and_restore_full_font(self):
        ip = '127.0.0.2'
        self.plotter.device_edits[scada.ISOMON_DEVICE_INDEX].setText(f'{ip}:10578')
        self.manager.add_device(ip)
        device = self.manager.devices[ip]
        device.is_isomon = True
        device.iso_live_valid = True
        device.iso_live_values = {field: -9_999_999.0 for field in (
            'u1_baseline', 'u2_baseline', 'u1_s2', 'u2_s2', 'u1_s3', 'u2_s3',
            'r1_via_r3', 'r2_via_r3', 'r1_via_r4', 'r2_via_r4')}
        self.plotter._refresh_analog_values()
        first_row = self.plotter.device_analog_value_labels[scada.ISOMON_DEVICE_INDEX][2]
        second_row = self.plotter.isomon_iso_row2_lbl

        for width, expected_font in ((1920, '16px'), (3800, '16px'), (1920, '16px')):
            self.plotter.resize(width, 800)
            self.plotter.show()
            self.app.processEvents()
            self.assertLessEqual(self.plotter.width(), width)
            for label in (first_row, second_row):
                self.assertLessEqual(label.mapTo(self.plotter, label.rect().topRight()).x(), width - 10)
                self.assertGreaterEqual(label.width() - label.indent(),
                                        label.fontMetrics().horizontalAdvance(label.text()))
                if width == 3800:
                    self.assertIn(expected_font, label.styleSheet())
                else:
                    self.assertNotIn(expected_font, label.styleSheet())

        with patch.object(self.plotter, '_poll_isomon_alg_enable'), \
             patch.object(self.plotter, '_refresh_isomon_debug_pins'):
            self.plotter.isomon_debug_chk.setChecked(True)
        for width in (1920, 3800, 1920):
            self.plotter.resize(width, 800)
            self.plotter.show()
            self.plotter._realign_isomon_row2_label()
            self.app.processEvents()
            debug_panel = self.plotter.isomon_debug_panel
            self.assertLessEqual(debug_panel.geometry().right(), self.plotter.width() - 10)
            self.assertGreater(debug_panel.x(), second_row.mapTo(
                self.plotter, second_row.rect().topRight()).x())
            for label in (first_row, second_row):
                self.assertGreaterEqual(label.width() - label.indent(),
                                        label.fontMetrics().horizontalAdvance(label.text()))

    def test_remote_control_layout_uses_two_top_rows(self):
        config_grid = self.plotter.layout().itemAt(0).layout()
        self.assertEqual(config_grid.getItemPosition(config_grid.indexOf(self.plotter.remote_control_enabled))[:2], (0, 9))
        self.assertEqual(config_grid.getItemPosition(config_grid.indexOf(self.plotter.remote_control_addr))[:2], (1, 9))
        token_position = config_grid.getItemPosition(config_grid.indexOf(self.plotter.remote_control_token))
        self.assertEqual(token_position[:2], (2, 9))
        self.assertEqual(token_position[3], 3)
        self.assertEqual(config_grid.itemAtPosition(0, 8).widget().text(), 'Remote Control')
        self.assertEqual(config_grid.itemAtPosition(1, 8).widget().text(), 'Web addr:port')
        self.assertEqual(config_grid.itemAtPosition(2, 8).widget().text(), 'Token')
        self.assertEqual(config_grid.itemAtPosition(3, 8).widget().text(), 'Receiver addr:port')
        self.assertEqual(config_grid.itemAtPosition(4, 8).widget().text(), 'Measurement number')
        self.assertEqual(config_grid.indexOf(self.plotter.save_calibration_btn), -1)
        button_row = self.plotter.layout().itemAt(1).layout()
        action_order = (
            'start_sampling', 'start_sampling_on_trigger', 'force_trigger',
            'save_measurement', 'save_calibration', 'reset_counter',
            'reset_latched_faults', 'reset_devices',
        )
        button_indices = [
            button_row.indexOf(self.plotter.findChild(QPushButton, name))
            for name in action_order
        ]
        self.assertTrue(all(index >= 0 for index in button_indices))
        self.assertEqual(button_indices, sorted(button_indices))
        self.assertEqual(button_row.indexOf(self.plotter.downsample_mode_combo), -1)
        statistics_row = self.plotter.layout().itemAt(3).layout()
        self.assertEqual(statistics_row.indexOf(self.plotter.error_lbl), 0)
        plot_control_widgets = (
            self.plotter.findChild(QLabel, 'downsample_label'),
            self.plotter.downsample_mode_combo,
            self.plotter.downsample_factor_spin,
            self.plotter.clip_to_view_chk,
        )
        control_positions = [statistics_row.indexOf(widget) for widget in plot_control_widgets]
        self.assertTrue(all(index > 0 for index in control_positions))
        self.assertEqual(control_positions, sorted(control_positions))
        statistics_top = statistics_row.itemAt(0).geometry().top()
        control_tops = [statistics_row.itemAt(index).geometry().top()
                for index in control_positions]
        self.assertTrue(all(top == statistics_top for top in control_tops))
        self.assertFalse(hasattr(self.plotter, 'remote_control_apply'))
        self.assertFalse(hasattr(self.plotter, 'remote_control_status'))

    def test_startup_remote_values_are_displayed_in_gui(self):
        previous_enabled = scada.REMOTE_CONTROL_ENABLED
        previous_address = scada.REMOTE_CONTROL_ADDR_PORT
        previous_token = scada.REMOTE_CONTROL_TOKEN
        manager = scada.DeviceManager()
        plotter = None
        try:
            scada.REMOTE_CONTROL_ENABLED = True
            scada.REMOTE_CONTROL_ADDR_PORT = '127.0.0.1:0'
            scada.REMOTE_CONTROL_TOKEN = 'startup-token'
            plotter = scada.Plotter(manager)
            self.assertTrue(plotter.remote_control_enabled.isChecked())
            self.assertEqual(plotter.remote_control_addr.text(), '127.0.0.1:0')
            self.assertEqual(plotter.remote_control_token.text(), 'startup-token')
            self.assertFalse(plotter.remote_control_addr.isEnabled())
            self.assertTrue(plotter._remote_server.running)
            plotter.remote_control_token.setText('runtime-token')
            self.assertTrue(plotter._remote_update_token())
            self.assertEqual(plotter._remote_server._token, 'runtime-token')
            token_snapshot = plotter._remote_widget_snapshot(
                'remote_control_token', plotter.remote_control_token)
            self.assertEqual(token_snapshot['text'], '********')
        finally:
            if plotter is not None:
                plotter._remote_stop()
                plotter.close()
            else:
                manager.shutdown()
            scada.REMOTE_CONTROL_ENABLED = previous_enabled
            scada.REMOTE_CONTROL_ADDR_PORT = previous_address
            scada.REMOTE_CONTROL_TOKEN = previous_token

    def test_screenshot_endpoint_returns_full_window_png(self):
        self.plotter.resize(960, 640)
        self.plotter.show()
        self.app.processEvents()
        expected_width = self.plotter.width()
        expected_height = self.plotter.height()
        status, response = self._request_on_gui_event_loop('/api/v1/screenshot')
        self.assertEqual(status, 200)
        image = response['data']
        self.assertEqual(image['mime_type'], 'image/png')
        self.assertEqual(image['encoding'], 'base64')
        png = base64.b64decode(image['data'], validate=True)
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        png_width = int.from_bytes(png[16:20], 'big')
        png_height = int.from_bytes(png[20:24], 'big')
        self.assertEqual(image['width'], png_width)
        self.assertEqual(image['height'], png_height)
        self.assertEqual(image['logical_width'], expected_width)
        self.assertEqual(image['logical_height'], expected_height)
        self.assertGreaterEqual(image['device_pixel_ratio'], 1)
        self.assertGreater(len(png), 1000)

    def test_shutdown_endpoint_acknowledges_then_closes_window(self):
        self.plotter.show()
        self.app.processEvents()
        status, response = self._request_on_gui_event_loop('/api/v1/application/shutdown', method='POST')
        self.assertEqual(status, 200)
        self.assertTrue(response['data']['accepted'])
        self.assertEqual(response['data']['delay_ms'], 500)

        deadline = time.monotonic() + 2
        while self.plotter.isVisible() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.plotter.isVisible())

    def test_measurement_routes_and_log_route_are_exposed(self):
        status, live = self._request_on_gui_event_loop('/api/v1/live')
        self.assertEqual(status, 200)
        self.assertIn('devices', live['data'])
        status, log = self._request_on_gui_event_loop('/api/v1/log?tail=2')
        self.assertEqual(status, 200)
        self.assertLessEqual(len(log['data']['lines']), 2)

    def test_plot_snapshot_and_view_control(self):
        status, plots = self._request_on_gui_event_loop('/api/v1/plots?points=100')
        self.assertEqual(status, 200)
        self.assertIn('signals', plots['data']['plots'])
        status, view = self._request_on_gui_event_loop(
            '/api/v1/plots/view', method='POST',
            body={'plot': 'signals', 'x_range': [-0.25, 0.75]})
        self.assertEqual(status, 200)
        self.assertEqual(view['data']['plot'], 'signals')
        self.assertAlmostEqual(view['data']['x_range'][0], -0.25, places=3)
        self.assertAlmostEqual(view['data']['x_range'][1], 0.75, places=3)

    def test_runtime_toggle_and_rebind(self):
        self.plotter.remote_control_enabled.setChecked(False)
        self.assertTrue(self.plotter.remote_control_addr.isEnabled())
        self.plotter.remote_control_addr.setText('127.0.0.1:0')
        self.plotter.remote_control_enabled.setChecked(True)
        self.assertTrue(self.plotter._remote_server.running)
        self.assertFalse(self.plotter.remote_control_addr.isEnabled())
        first_address = self.plotter._remote_server.address

        self.plotter.remote_control_enabled.setChecked(False)
        self.assertTrue(self.plotter.remote_control_addr.isEnabled())
        self.assertFalse(self.plotter._remote_server.running)

        self.plotter.remote_control_addr.setText('not-an-address')
        self.plotter.remote_control_enabled.setChecked(True)
        self.assertFalse(self.plotter.remote_control_enabled.isChecked())
        self.assertFalse(self.plotter._remote_server.running)
        self.assertTrue(self.plotter.remote_control_addr.isEnabled())

        self.plotter.remote_control_addr.setText('127.0.0.1:0')
        self.plotter.remote_control_enabled.setChecked(True)
        self.assertTrue(self.plotter._remote_server.running)
        self.assertNotEqual(first_address, self.plotter._remote_server.address)

        self.plotter.remote_control_enabled.setChecked(False)
        self.assertFalse(self.plotter._remote_server.running)
        with self.assertRaises(scada.APIError):
            self.plotter._remote_dispatch_gui('status', {})

    def test_invalid_trigger_window_is_atomic(self):
        self.plotter._stream_specs = lambda: [('device', 'data', 1000)]
        old_values = (self.plotter.pretrigger_spin.value(), self.plotter.sample_spin.value())
        with self.assertRaises(scada.APIError):
            self.plotter._remote_dispatch_gui('measurement_start', {
                'mode': 'immediate', 'pretrigger_ms': 500, 'posttrigger_ms': 30000})
        self.assertEqual(
            (self.plotter.pretrigger_spin.value(), self.plotter.sample_spin.value()), old_values)


if __name__ == '__main__':
    unittest.main()
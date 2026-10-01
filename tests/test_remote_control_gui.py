"""Offscreen integration test for the HTTP-to-Qt GUI bridge."""
import json
import os
import threading
import time
import unittest
from urllib.request import Request, urlopen

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import scada
from PyQt5.QtWidgets import QApplication
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

    def test_remote_control_layout_uses_two_top_rows(self):
        config_grid = self.plotter.layout().itemAt(0).layout()
        self.assertEqual(config_grid.getItemPosition(config_grid.indexOf(self.plotter.remote_control_enabled))[:2], (0, 9))
        self.assertEqual(config_grid.getItemPosition(config_grid.indexOf(self.plotter.remote_control_addr))[:2], (1, 9))
        self.assertEqual(config_grid.itemAtPosition(0, 8).widget().text(), 'Remote Control')
        self.assertEqual(config_grid.itemAtPosition(1, 8).widget().text(), 'Web addr:port')
        self.assertFalse(hasattr(self.plotter, 'remote_control_apply'))
        self.assertFalse(hasattr(self.plotter, 'remote_control_status'))

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
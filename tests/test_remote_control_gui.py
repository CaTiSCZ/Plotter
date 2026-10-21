"""Offscreen integration test for the HTTP-to-Qt GUI bridge."""
import json
import base64
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import scada
from PyQt5.QtCore import Qt, QCoreApplication, QEvent
from PyQt5.QtGui import QTextCursor, QTextDocument
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

    def test_save_calibration_manual_and_remote(self):
        ip = '127.0.0.2'
        self.manager.add_device(ip)
        calibration = {'channels': [{'unit': 'V', 'gain': 2.0, 'offset': 0.5}]}
        os.makedirs('RICE_mereni', exist_ok=True)
        with tempfile.TemporaryDirectory(dir='RICE_mereni') as directory, \
             patch.object(self.plotter, '_calibration_from_loaded_id', return_value=calibration), \
             patch.object(self.plotter, '_read_alg_config_snapshot', return_value={}), \
             patch.object(self.plotter, '_identity_from_loaded_id', return_value={}), \
             patch.object(scada.QMessageBox, 'critical') as error_dialog:
            for mode in ('manual', 'remote'):
                with self.subTest(mode=mode):
                    path = os.path.join(directory, f'{mode}.json')
                    with patch.object(scada.QFileDialog, 'getSaveFileName',
                                      return_value=(path, 'JSON Files (*.json)')) as dialog:
                        if mode == 'manual':
                            self.plotter.save_calibration_btn.click()
                            dialog.assert_called_once()
                        else:
                            status, _ = self._request_on_gui_event_loop(
                                '/api/v1/widgets/save_calibration', method='POST',
                                body={'action': 'save_to_path', 'path': path})
                            self.assertEqual(status, 200)
                            dialog.assert_not_called()
                    with open(path, encoding='utf-8') as saved_file:
                        payload = json.load(saved_file)
                    self.assertEqual(payload['format'], 'fdds_scada_calibration_alg_config_v1')
                    self.assertEqual(payload['devices'][0]['ip'], ip)
                    self.assertEqual(payload['devices'][0]['calibration'], calibration)
            error_dialog.assert_not_called()

    def test_save_calibration_manual_without_devices(self):
        with patch.object(scada.QMessageBox, 'warning') as warning, \
             patch.object(scada.QFileDialog, 'getSaveFileName') as dialog:
            self.plotter.save_calibration_btn.click()
            warning.assert_called_once()
            dialog.assert_not_called()

    def test_save_calibration_manual_cancel(self):
        self.manager.add_device('127.0.0.2')
        with patch.object(scada.QFileDialog, 'getSaveFileName', return_value=('', '')) as dialog, \
             patch.object(self.plotter, '_calibration_from_loaded_id') as calibration:
            self.plotter.save_calibration_btn.click()
            dialog.assert_called_once()
            calibration.assert_not_called()

    def test_settings_windows_are_modeless_and_independent_per_device(self):
        def live_config(device_type, section_id):
            tag = scada.fdds_alg_config.DEVICE_TABLES[device_type]['tag']
            if section_id == 0xFF:
                return {
                    'tag': tag,
                    'schema': 1,
                    'section': section_id,
                    'body': b'',
                }
            section = scada.fdds_alg_config.section_by_id(device_type, section_id)
            values = {}
            for name, count, *rest in section.fields:
                value = 1000.0 if device_type == 'isomon' and name in (
                    'r3', 'r4', 'r5', 'r6', 'r7', 'r8') else 0
                if not rest or rest[0] == 'f':
                    value = float(value)
                values[name] = [value] * count if count > 1 else value
            return {
                'tag': tag,
                'schema': 1,
                'section': section_id,
                'body': section.pack(values),
            }

        for row, ip, device_type in (
                (0, '127.0.0.2', 'node'),
                (scada.ISOMON_DEVICE_INDEX, '127.0.0.3', 'isomon')):
            self.plotter.device_edits[row].setText(f'{ip}:10578')
            self.manager.add_device(ip)
            device = self.manager.devices[ip]
            device.role = device_type
            device.is_isomon = device_type == 'isomon'
            device.get_calibration = lambda source=0, count=(2 if device_type == 'isomon' else 1): {
                'source': source,
                'channels': [
                    {'unit': 'V', 'offset': 0.0, 'gain': 1.0}
                    for _ in range(count)
                ],
                'calibration_info': {'time': 0, 'id': 7},
            }
            device.get_alg_config_section = (
                lambda section_id, kind=device_type, read=live_config:
                read(kind, section_id))
            self.plotter.device_settings_buttons[row].click()

        deadline = time.monotonic() + 3
        while (time.monotonic() < deadline and
               any(not dialog._loaded for dialog in self.plotter._settings_dialogs.values())):
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()

        self.assertEqual(len(self.plotter._settings_dialogs), 2)
        try:
            for ip, dialog in self.plotter._settings_dialogs.items():
                self.assertTrue(dialog._loaded)
                self.assertEqual(dialog._source, 'effective')
                self.assertEqual(dialog.windowModality(), Qt.NonModal)
                self.assertIn(ip, dialog.windowTitle())
                self.assertEqual(
                    len(dialog._channel_widgets), 2 if ip == '127.0.0.3' else 1)
            isomon = self.plotter._settings_dialogs['127.0.0.3']
            self.assertEqual(isomon._device_type, 'isomon')
            imported_channels = [
                {'unit': 'V', 'offset': 2.656798363, 'gain': 0.002133012},
                {'unit': 'V', 'offset': 32.016990662, 'gain': 0.002108254},
            ]
            imported_payload = {
                'format': 'fdds_scada_device_settings_v1',
                'device': {'type': 'isomon'},
                'calibration': imported_channels,
                'alg_config': {
                    'device': 'isomon',
                    'sections': isomon._sections,
                },
            }
            with tempfile.NamedTemporaryFile(
                    'w', suffix='.json', encoding='utf-8', delete=False) as source_file:
                json.dump(imported_payload, source_file)
                imported_path = source_file.name
            try:
                with patch.object(
                        scada.QFileDialog, 'getOpenFileName',
                        return_value=(imported_path, 'JSON Files (*.json)')):
                    isomon._import_values()
                self.assertEqual(isomon._calibration['channels'], imported_channels)
                self.assertEqual(len(isomon._channel_widgets), 2)
                self.assertAlmostEqual(isomon._channel_widgets[0][1].value(), 2.656798363)
                self.assertAlmostEqual(isomon._channel_widgets[1][1].value(), 32.016990662)
                self.assertTrue(all(isomon._isomon_resistor_widgets[name].value() == 1000.0
                                    for name in ('r3', 'r4', 'r5', 'r6', 'r7', 'r8')))
                self.assertTrue(isomon._dirty)
            finally:
                os.unlink(imported_path)

            bundle_channels = [
                {'unit': 'A', 'offset': 1.25, 'gain': 0.5},
                {'unit': 'mV', 'offset': -2.5, 'gain': 0.25},
            ]
            bundle_resistors = {
                name: value for name, value in zip(
                    ('r3', 'r4', 'r5', 'r6', 'r7', 'r8'),
                    (3.0, 4.0, 5.0, 6.0, 7.0, 8.0))
            }
            bundle_payload = {
                'format': 'fdds_scada_calibration_alg_config_v1',
                'devices': [
                    {
                        'ip': '127.0.0.99',
                        'calibration': {'channels': []},
                        'alg_config': {
                            'device': 'isomon',
                            'sections': isomon._sections,
                        },
                    },
                    {
                        'ip': '127.0.0.3',
                        'calibration': {
                            'channels': bundle_channels,
                            'calibration_info': {'time': 0, 'id': 11},
                            'isomon_resistors': bundle_resistors,
                        },
                        'alg_config': {
                            'device': 'isomon',
                            'sections': isomon._sections,
                        },
                    },
                ],
            }
            isomon._dirty = False
            with tempfile.NamedTemporaryFile(
                    'w', suffix='.json', encoding='utf-8', delete=False) as source_file:
                json.dump(bundle_payload, source_file)
                bundle_path = source_file.name
            try:
                with patch.object(
                        scada.QFileDialog, 'getOpenFileName',
                        return_value=(bundle_path, 'JSON Files (*.json)')):
                    isomon._import_values()
                self.assertEqual(isomon._calibration['channels'], bundle_channels)
                self.assertEqual(isomon._calibration['calibration_info']['id'], 11)
                self.assertEqual(
                    isomon._calibration['isomon_resistors'], bundle_resistors)
                self.assertEqual(isomon._channel_widgets[0][0].text(), 'A')
                self.assertAlmostEqual(isomon._channel_widgets[0][1].value(), 1.25)
                self.assertEqual(isomon._channel_widgets[1][0].text(), 'mV')
                self.assertAlmostEqual(isomon._channel_widgets[1][1].value(), -2.5)
                self.assertEqual(
                    {name: editor.value()
                     for name, editor in isomon._isomon_resistor_widgets.items()},
                    bundle_resistors)
                calibration_values, _ = isomon._collect_values()
                self.assertEqual(
                    calibration_values['isomon_resistors'], bundle_resistors)
                self.assertTrue(isomon._dirty)
            finally:
                os.unlink(bundle_path)

            first = self.plotter._settings_dialogs['127.0.0.2']
            first._field_widgets['node_dv_filt']['dv_refSourceVoltage'][0].setText('12.5')
            self.assertTrue(first._dirty)
            self.assertIn('zaps', first.status_label.text())
        finally:
            for dialog in list(self.plotter._settings_dialogs.values()):
                dialog._dirty = False
                dialog.close()

    def test_isomon_calibration_codec_includes_resistor_values(self):
        device = scada.Device(
            '127.0.0.20', scada.DEFAULT_CMD_PORT, scada.DEFAULT_DATA_PORT,
            None, manager=self.manager)
        try:
            channels = [
                {'unit': 'V', 'offset': 1.5, 'gain': 0.25},
                {'unit': 'mV', 'offset': -2.0, 'gain': 0.5},
            ]
            resistor_values = {
                name: value for name, value in zip(
                    ('r3', 'r4', 'r5', 'r6', 'r7', 'r8'),
                    (3.0, 4.0, 5.0, 6.0, 7.0, 8.0))
            }
            captured = {}

            def send_ack(command, payload=b''):
                captured['command'] = command
                captured['payload'] = payload
                return 0, b'\xff'

            device._send_cmd_with_ack = send_ack
            self.assertTrue(device.set_calibration(
                channels, save=False,
                calibration_info={'time': 123, 'id': 9},
                isomon_resistors=resistor_values))
            payload = captured['payload']
            resistor_offset = 4 + len(channels) * scada.STRUCT.CHANNEL.size + 12
            self.assertEqual(
                scada.struct.unpack_from('<6f', payload, resistor_offset),
                tuple(float(resistor_values[name])
                      for name in ('r3', 'r4', 'r5', 'r6', 'r7', 'r8')))

            response = (
                bytes((0,)) + scada.struct.pack('<H', len(channels)) +
                b''.join(scada.STRUCT.CHANNEL.pack(
                    channel['unit'].encode(), channel['offset'], channel['gain'])
                         for channel in channels) +
                scada.STRUCT.CALIBRATION_INFO.pack(123, 9) +
                scada.struct.pack('<6f', *resistor_values.values())
            )
            device._send_cmd_with_ack = lambda command, payload=b'': (0, response)
            parsed = device.get_calibration(source=0)
            self.assertEqual(parsed['channels'], channels)
            self.assertEqual(parsed['calibration_info'], {'time': 123, 'id': 9})
            self.assertEqual(parsed['isomon_resistors'], resistor_values)
        finally:
            device.cmd_sock.close()

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

    def test_isomon_measured_voltage_decimal_points_and_units_align(self):
        ip = '127.0.0.2'
        self.plotter.device_edits[scada.ISOMON_DEVICE_INDEX].setText(f'{ip}:10578')
        self.manager.add_device(ip)
        device = self.manager.devices[ip]
        device.iso_live_valid = True
        device.iso_live_values = dict(zip(
            (*scada.ISO_U_FIELDS, *scada.ISO_R_FIELDS),
            (289.107, -314.010, 89.825, -508.559, 510.766, -97.574,
             20_166_000.0, 81_331_000.0, 24_128_000.0, 208_400_000.0)))
        device.last_analog_valid = True
        device.analog_units = ['V', 'V']
        device.analog_widths = [7, 8]
        self.plotter.resize(3800, 800)
        self.plotter.show()

        def character_x(label, character):
            document = QTextDocument()
            document.documentLayout().setPaintDevice(label)
            document.setDocumentMargin(0)
            document.setDefaultFont(label.font())
            document.setHtml(label.text())
            text = document.toPlainText()
            position = text.rindex(character)
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            block = cursor.block()
            layout = block.layout()
            return layout.lineForTextPosition(cursor.positionInBlock()).cursorToX(
                cursor.positionInBlock())[0]

        for values, decimals in (
                ([94.054, -504.390], [3, 3]),
                ([-1504.123, 0.001], [3, 3]),
                ([94.05, -504.390], [2, 3])):
            device.last_analog_values = values
            device.analog_decimals = decimals
            analog_values = [self.plotter._format_analog_value(device, idx) for idx in range(2)]
            self.plotter._refresh_analog_values()
            self.app.processEvents()
            for character in ('.', 'V'):
                self.assertAlmostEqual(
                    character_x(self.plotter.isomon_iso_row1_lbl, character),
                    character_x(self.plotter.isomon_iso_row2_lbl, character), delta=0.5)
            self.assertEqual(analog_values, [
                self.plotter._format_analog_value(device, idx) for idx in range(2)])

        for valid in (False, True):
            device.last_analog_valid = valid
            device.last_analog_values = [float('nan'), -504.390]
            self.plotter._refresh_analog_values()
            self.app.processEvents()
            self.assertAlmostEqual(
                character_x(self.plotter.isomon_iso_row1_lbl, 'V'),
                character_x(self.plotter.isomon_iso_row2_lbl, 'V'), delta=0.5)

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
        device.last_analog_valid = True
        device.last_analog_values = [1.25, -2.5]
        device.analog_units = ['V', 'V']
        self.plotter._refresh_analog_values()
        first_row = self.plotter.device_analog_value_labels[scada.ISOMON_DEVICE_INDEX][2]
        second_row = self.plotter.isomon_iso_row2_lbl
        first_row_text = first_row.text()
        second_row_text = second_row.text()
        self.assertNotIn('R1 <sub>', first_row_text)
        self.assertIn('R1<sub>R3</sub>', first_row_text)
        self.assertIn('U1<sub>base</sub>', first_row_text)
        self.assertIn('U<sub>POS</sub>', first_row_text)
        self.assertIn('R1<sub>R4</sub>', second_row_text)
        self.assertIn('U2<sub>S2</sub>', second_row_text)
        self.assertIn('U<sub>NEG</sub>', second_row_text)
        self.assertIn('1.250 V', first_row.property('iso_plain_text'))
        self.assertIn('-2.500 V', second_row.property('iso_plain_text'))
        self.plotter.resize(1920, 800)
        self.plotter.show()
        self.app.processEvents()
        controls = self.plotter.device_settings_buttons[scada.ISOMON_DEVICE_INDEX].parentWidget()
        controls_right = controls.mapTo(self.plotter, controls.rect().topRight()).x()
        controls_y = controls.mapTo(self.plotter, controls.rect().center()).y()
        self.assertGreater(first_row.mapTo(self.plotter, first_row.rect().topLeft()).x(), controls_right)
        self.assertLessEqual(first_row.mapTo(self.plotter, first_row.rect().topLeft()).y(), controls_y)
        self.assertGreaterEqual(first_row.mapTo(self.plotter, first_row.rect().bottomLeft()).y(), controls_y)
        for revision in range(20):
            device.iso_live_values['r1_via_r3'] = 11_077_000.0 + revision
            self.plotter._refresh_analog_values()
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertIs(self.plotter.device_analog_value_labels[scada.ISOMON_DEVICE_INDEX][2], first_row)
            for label in (first_row, second_row):
                self.assertTrue(label.isVisible())
                self.assertGreater(label.width(), 0)
                self.assertGreater(label.height(), 0)
        self.plotter._rebuild_analog_value_row(scada.ISOMON_DEVICE_INDEX, device.channels)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.plotter._refresh_analog_values()
        self.app.processEvents()
        self.assertIs(self.plotter.device_analog_value_labels[scada.ISOMON_DEVICE_INDEX][2], first_row)

        def assert_rows_render_in_place():
            for label in (first_row, second_row):
                self.assertTrue(label.isVisible())
                document = QTextDocument()
                document.documentLayout().setPaintDevice(label)
                document.setDocumentMargin(0)
                document.setDefaultFont(label.font())
                document.setHtml(label.text())
                self.assertLessEqual(document.idealWidth(), label.width())
                self.assertLessEqual(document.size().height(), label.height())
                self.assertGreater(label.mapTo(self.plotter, label.rect().topLeft()).x(), controls_right)
                self.assertLessEqual(label.mapTo(self.plotter, label.rect().topRight()).x(),
                                     self.plotter.width() - 10)
            self.assertEqual(first_row.mapTo(self.plotter, first_row.rect().topLeft()).x(),
                             second_row.mapTo(self.plotter, second_row.rect().topLeft()).x())
            self.assertLess(first_row.mapTo(self.plotter, first_row.rect().bottomLeft()).y(),
                            second_row.mapTo(self.plotter, second_row.rect().topLeft()).y())

        for width in (1920, 3800, 1920):
            self.plotter.resize(width, 800)
            self.plotter.show()
            self.app.processEvents()
            self.assertLessEqual(self.plotter.width(), width, f'requested={width}, min={self.plotter.minimumWidth()}')
            assert_rows_render_in_place()
            if width == 3800:
                for label in (first_row, second_row):
                    self.assertIn('16px', label.styleSheet())

        with patch.object(self.plotter, '_poll_isomon_alg_enable'), \
             patch.object(self.plotter, '_refresh_isomon_debug_pins'):
            self.plotter.isomon_debug_chk.setChecked(True)
        for width in (1920, 3800, 1920):
            self.plotter.resize(width, 800)
            self.plotter.show()
            self.plotter._realign_isomon_row2_label()
            self.app.processEvents()
            debug_panel = self.plotter.isomon_debug_panel
            assert_rows_render_in_place()
            self.assertLessEqual(debug_panel.geometry().right(), self.plotter.width() - 10)
            document = QTextDocument()
            document.documentLayout().setPaintDevice(second_row)
            document.setDocumentMargin(0)
            document.setDefaultFont(second_row.font())
            document.setHtml(second_row.text())
            second_row_right = second_row.mapTo(
                self.plotter, second_row.rect().topLeft()).x() + document.idealWidth()
            if debug_panel.y() == first_row.mapTo(self.plotter, first_row.rect().topLeft()).y():
                self.assertGreater(debug_panel.x(), second_row_right)
            else:
                self.assertGreater(debug_panel.y(), second_row.mapTo(
                    self.plotter, second_row.rect().bottomLeft()).y())

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
        for row, button in enumerate(self.plotter.device_settings_buttons):
            self.assertEqual(
                config_grid.getItemPosition(config_grid.indexOf(button.parentWidget()))[:2], (row, 6))
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
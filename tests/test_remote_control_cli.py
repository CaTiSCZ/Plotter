"""Tests for the remote-control command-line override."""
import types
import unittest

import scada


class RemoteControlCliTests(unittest.TestCase):
    def test_remote_control_option_is_removed_from_qt_arguments(self):
        options, qt_argv = scada._parse_cli_args([
            'scada.py', '--remote_control', '192.168.1.20:8765', 'DEBUG'])
        self.assertEqual(options.remote_control, '192.168.1.20:8765')
        self.assertEqual(qt_argv, ['scada.py', 'DEBUG'])

    def test_cli_address_enables_remote_and_overrides_settings(self):
        settings = types.SimpleNamespace(
            REMOTE_CONTROL_ENABLED=False,
            REMOTE_CONTROL_ADDR_PORT='127.0.0.1:8080',
            REMOTE_CONTROL_TOKEN='secret-token',
        )
        result = scada._remote_control_startup_settings(settings, '192.168.1.20:8765')
        self.assertEqual(result, (True, '192.168.1.20:8765', 'secret-token'))

    def test_settings_are_used_without_cli_override(self):
        settings = types.SimpleNamespace(
            REMOTE_CONTROL_ENABLED=True,
            REMOTE_CONTROL_ADDR_PORT='127.0.0.1:9000',
            REMOTE_CONTROL_TOKEN='',
        )
        self.assertEqual(
            scada._remote_control_startup_settings(settings),
            (True, '127.0.0.1:9000', ''),
        )


if __name__ == '__main__':
    unittest.main()
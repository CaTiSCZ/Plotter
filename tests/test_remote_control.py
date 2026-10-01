"""Focused tests for the stdlib remote-control HTTP transport."""
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from remote_control import RemoteControlServer


class RemoteControlServerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.server = RemoteControlServer(self.dispatch, token="test-token")
        self.address = self.server.start("127.0.0.1:0")
        host, port = self.address.rsplit(":", 1)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.stop()

    def dispatch(self, operation, arguments):
        self.calls.append((operation, arguments, threading.current_thread().name))
        return {"operation": operation, "arguments": arguments}

    def request(self, path, *, method="GET", body=None, token="test-token"):
        headers = {}
        data = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(req, timeout=2)
        except HTTPError as exc:
            response = exc
        try:
            return response.status, json.loads(response.read())
        finally:
            response.close()

    def test_get_dispatches_and_returns_envelope(self):
        status, payload = self.request("/api/v1/ui/state")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["operation"], "ui_state")
        self.assertEqual(self.calls[0][0], "ui_state")
        self.assertNotEqual(self.calls[0][2], threading.current_thread().name)

    def test_widget_action_decodes_id_and_body(self):
        status, payload = self.request(
            "/api/v1/widgets/device%3A0", method="POST", body={"action": "click"})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1][0], "widget_action")
        self.assertEqual(self.calls[-1][1], {"widget_id": "device:0", "action": "click"})

    def test_bad_token_is_rejected(self):
        status, payload = self.request("/api/v1/status", token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(self.calls, [])

    def test_token_can_be_changed_while_server_is_running(self):
        self.server.set_token('replacement-token')
        status, payload = self.request('/api/v1/status', token='test-token')
        self.assertEqual(status, 401)
        self.assertEqual(payload['error']['code'], 'unauthorized')
        status, payload = self.request('/api/v1/status', token='replacement-token')
        self.assertEqual(status, 200)

    def test_log_tail_is_validated(self):
        status, payload = self.request("/api/v1/log?tail=1001")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_tail")

    def test_measurement_and_plot_routes_dispatch(self):
        for path, expected in (
            ("/api/v1/measurement/start", "measurement_start"),
            ("/api/v1/measurement/stop", "measurement_stop"),
            ("/api/v1/measurement/save", "measurement_save"),
            ("/api/v1/application/shutdown", "application_shutdown"),
            ("/api/v1/plots/view", "plots_view"),
        ):
            status, _ = self.request(path, method="POST", body={})
            self.assertEqual(status, 200)
            self.assertEqual(self.calls[-1][0], expected)

        status, _ = self.request("/api/v1/screenshot")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1][0], "screenshot")

    def test_plot_point_limit_is_validated(self):
        status, payload = self.request("/api/v1/plots?points=5001")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_points")

    def test_non_loopback_requires_token(self):
        server = RemoteControlServer(self.dispatch)
        with self.assertRaisesRegex(ValueError, "bearer token"):
            server.start("0.0.0.0:0")

    def test_stop_and_restart(self):
        self.server.stop()
        self.assertFalse(self.server.running)
        self.address = self.server.start("127.0.0.1:0")
        self.assertTrue(self.server.running)


if __name__ == "__main__":
    unittest.main()
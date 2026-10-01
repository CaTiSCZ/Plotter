"""Small HTTP transport for the SCADA remote-control API.

The transport is deliberately independent of Qt. Its dispatcher is expected to
hand requests to the GUI thread and return a JSON-serializable result.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import logging
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit


API_PREFIX = "/api/v1"
MAX_REQUEST_BYTES = 64 * 1024


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class RemoteControlServer:
    """Startable/rebindable HTTP server which delegates work to a dispatcher."""

    def __init__(self, dispatcher, *, token: str = "", logger=None):
        self._dispatcher = dispatcher
        self._token = str(token or "")
        self._logger = logger or logging.getLogger(__name__)
        self._server = None
        self._thread = None
        self._auth_state = None
        self._auth_lock = threading.Lock()
        self._lock = threading.RLock()

    @staticmethod
    def _is_loopback_or_unspecified(host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return address.version == 4 and address.is_loopback

    @property
    def address(self):
        with self._lock:
            if self._server is None:
                return None
            host, port = self._server.server_address[:2]
            return f"{host}:{port}"

    @property
    def running(self):
        with self._lock:
            return self._server is not None

    def set_token(self, token: str):
        token = str(token or '')
        with self._lock:
            if self._server is not None:
                host = self._server.server_address[0]
                if not self._is_loopback_or_unspecified(host) and not token:
                    raise ValueError('A bearer token is required for non-loopback binds')
            self._token = token
            if self._auth_state is not None:
                with self._auth_lock:
                    self._auth_state['token'] = token

    def start(self, address: str):
        host, separator, port_text = str(address).strip().rpartition(":")
        if not separator or not host or not port_text.isdigit():
            raise ValueError("Bind address must use addr:port format")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Bind address must use an IPv4 address") from exc
        if ip.version != 4:
            raise ValueError("Bind address must use an IPv4 address")
        port = int(port_text)
        if not 0 <= port <= 65535:
            raise ValueError("Port must be between 0 and 65535")
        if not self._is_loopback_or_unspecified(host) and not self._token:
            raise ValueError("A bearer token is required for non-loopback binds")
        with self._lock:
            if self._server is not None:
                raise RuntimeError("HTTP server is already running")
            dispatcher = self._dispatcher
            auth_state = {'token': self._token}
            logger = self._logger
            request_slots = threading.BoundedSemaphore(8)

            class Handler(BaseHTTPRequestHandler):
                server_version = "FDDSRemoteControl/1"
                sys_version = ""

                def log_message(self, format, *args):
                    logger.debug("remote-http %s - %s", self.address_string(), format % args)

                def _send(self, status, payload, request_id):
                    body = json.dumps(
                        {"api_version": 1, "request_id": request_id, **payload},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

                def _authorized(self):
                    with self_outer._auth_lock:
                        expected_token = auth_state['token']
                    if not expected_token:
                        return True
                    scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
                    return scheme.lower() == "bearer" and hmac.compare_digest(supplied, expected_token)

                def _read_json(self):
                    raw_length = self.headers.get("Content-Length", "0")
                    try:
                        length = int(raw_length)
                    except ValueError as exc:
                        raise APIError(400, "invalid_content_length", "Content-Length must be an integer") from exc
                    if length < 0 or length > MAX_REQUEST_BYTES:
                        raise APIError(413, "request_too_large", "Request body exceeds the size limit")
                    if length == 0:
                        return {}
                    try:
                        value = json.loads(self.rfile.read(length))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise APIError(400, "invalid_json", "Request body must be valid JSON") from exc
                    if not isinstance(value, dict):
                        raise APIError(400, "invalid_json_shape", "Request JSON must be an object")
                    return value

                def _dispatch(self, method):
                    request_id = uuid.uuid4().hex
                    started_at = time.monotonic()
                    operation = method
                    audit_label = method
                    if not self._authorized():
                        self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "Bearer token required"}}, request_id)
                        logger.warning("remote request %s operation=%s result=unauthorized duration_ms=%.1f",
                                       request_id, operation, (time.monotonic() - started_at) * 1000)
                        return
                    try:
                        parsed = urlsplit(self.path)
                        path = parsed.path
                        if method == "GET" and path == f"{API_PREFIX}/ui/state":
                            operation, arguments = "ui_state", {}
                        elif method == "GET" and path == f"{API_PREFIX}/screenshot":
                            operation, arguments = "screenshot", {}
                        elif method == "GET" and path == f"{API_PREFIX}/statistics":
                            operation, arguments = "statistics", {}
                        elif method == "GET" and path == f"{API_PREFIX}/live":
                            operation, arguments = "live", {}
                        elif method == "GET" and path == f"{API_PREFIX}/plots":
                            query = parse_qs(parsed.query)
                            try:
                                points = int(query.get("points", ["1000"])[0])
                            except ValueError as exc:
                                raise APIError(400, "invalid_points", "points must be an integer") from exc
                            if not 10 <= points <= 5000:
                                raise APIError(400, "invalid_points", "points must be between 10 and 5000")
                            operation, arguments = "plots", {"points": points}
                        elif method == "GET" and path == f"{API_PREFIX}/log":
                            query = parse_qs(parsed.query)
                            try:
                                tail = int(query.get("tail", ["100"])[0])
                            except ValueError as exc:
                                raise APIError(400, "invalid_tail", "tail must be an integer") from exc
                            if not 1 <= tail <= 1000:
                                raise APIError(400, "invalid_tail", "tail must be between 1 and 1000")
                            operation, arguments = "log", {"tail": tail}
                        elif method == "GET" and path == f"{API_PREFIX}/status":
                            operation, arguments = "status", {}
                        elif method == "POST" and path == f"{API_PREFIX}/measurement/start":
                            operation, arguments = "measurement_start", self._read_json()
                        elif method == "POST" and path == f"{API_PREFIX}/measurement/stop":
                            operation, arguments = "measurement_stop", {}
                        elif method == "POST" and path == f"{API_PREFIX}/measurement/save":
                            operation, arguments = "measurement_save", {}
                        elif method == "POST" and path == f"{API_PREFIX}/application/shutdown":
                            operation, arguments = "application_shutdown", {}
                        elif method == "POST" and path == f"{API_PREFIX}/plots/view":
                            operation, arguments = "plots_view", self._read_json()
                        elif method == "POST" and path.startswith(f"{API_PREFIX}/widgets/"):
                            widget_id = unquote(path[len(f"{API_PREFIX}/widgets/"):])
                            if not widget_id or "/" in widget_id:
                                raise APIError(404, "widget_not_found", "Unknown widget ID")
                            operation, arguments = "widget_action", {**self._read_json(), "widget_id": widget_id}
                        else:
                            raise APIError(404, "not_found", "Unknown API endpoint")

                        audit_label = operation
                        if operation == "widget_action":
                            audit_label = f"widget:{arguments['widget_id']}:{arguments.get('action', '')}"
                        result = dispatcher(operation, arguments)
                        self._send(200, {"ok": True, "data": result}, request_id)
                        logger.info("remote request %s action=%s result=ok duration_ms=%.1f",
                                    request_id, audit_label, (time.monotonic() - started_at) * 1000)
                    except APIError as exc:
                        self._send(exc.status, {"ok": False, "error": {"code": exc.code, "message": str(exc)}}, request_id)
                        logger.info("remote request %s action=%s result=%s duration_ms=%.1f",
                                    request_id, audit_label, exc.code, (time.monotonic() - started_at) * 1000)
                    except TimeoutError as exc:
                        self._send(504, {"ok": False, "error": {"code": "timeout", "message": str(exc)}}, request_id)
                        logger.warning("remote request %s operation=%s result=timeout duration_ms=%.1f",
                                       request_id, operation, (time.monotonic() - started_at) * 1000)
                    except KeyError as exc:
                        self._send(404, {"ok": False, "error": {"code": "not_found", "message": str(exc)}}, request_id)
                        logger.info("remote request %s operation=%s result=not_found duration_ms=%.1f",
                                    request_id, operation, (time.monotonic() - started_at) * 1000)
                    except ValueError as exc:
                        self._send(400, {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}, request_id)
                        logger.info("remote request %s operation=%s result=invalid_request duration_ms=%.1f",
                                    request_id, operation, (time.monotonic() - started_at) * 1000)
                    except Exception:
                        logger.exception("remote request %s operation=%s failed", request_id, method)
                        self._send(500, {"ok": False, "error": {"code": "internal_error", "message": "Request failed"}}, request_id)
                        logger.error("remote request %s operation=%s result=internal_error duration_ms=%.1f",
                                     request_id, operation, (time.monotonic() - started_at) * 1000)

                def _handle_request(self, method):
                    if not request_slots.acquire(blocking=False):
                        self._send(503, {"ok": False, "error": {"code": "busy", "message": "Too many concurrent requests"}}, uuid.uuid4().hex)
                        return
                    try:
                        self._dispatch(method)
                    finally:
                        request_slots.release()

                def do_GET(self):
                    self._handle_request("GET")

                def do_POST(self):
                    self._handle_request("POST")

            self_outer = self
            server = ThreadingHTTPServer((host, port), Handler)
            server.daemon_threads = True
            server_thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="scada-http",
                daemon=True,
            )
            self._server = server
            self._thread = server_thread
            self._auth_state = auth_state
            server_thread.start()
            return self.address

    def stop(self):
        with self._lock:
            server, thread = self._server, self._thread
            if server is None:
                return
            self._server = None
            self._thread = None
            self._auth_state = None
        server.shutdown()
        server.server_close()
        if thread is not threading.current_thread():
            thread.join(timeout=2)
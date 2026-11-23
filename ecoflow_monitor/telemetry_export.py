from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
import time

from ecoflow_monitor.diagnostics import normalized_field, sanitize, sensitive_field


LOOPBACK = "127.0.0.1"
SNAPSHOT_PATH = "/v1/telemetry"
STREAMS = {
    "/v1/telemetry/stream": "telemetry",
    "/v1/telemetry/raw": "raw",
    "/v1/commands": "commands",
}
MAX_FRAME = 65536
MAX_CLIENTS = 16
MAX_STREAMS = 8
MAX_PENDING = 32
SERIAL_FIELDS = frozenset({"sn", "serial", "serialnumber", "devicesn", "deviceserial"})
SECRET_FIELDS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "secret", "passwd", "sign", "nonce",
    "signature", "credential", "credentials", "credentialref", "accessref", "secretref", "vault",
    "vaultid", "itemid", "privatekey", "certificate", "sessionkey", "sessionid", "brokerpassword",
    "mqttaccount", "mqttusername", "certificateaccount", "certification",
})
SECRET_SUFFIXES = (
    "accesskey", "secretkey", "apikey", "secret", "password", "passwd", "token", "privatekey", "signature", "credentialref",
)
logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encoded(data) -> bytes:
    return json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def sanitize_export(payload, preferences, *, serial="", device_name="", secrets=()):
    def protect(key):
        field = normalized_field(key)
        leaf = key.rsplit(".", 1)[-1]
        if normalized_field(leaf) in SERIAL_FIELDS or field.endswith(("serialnumber", "devicesn", "deviceserial")):
            return not preferences.show_serial
        if field in SECRET_FIELDS or normalized_field(leaf) in SECRET_FIELDS or field.endswith(SECRET_SUFFIXES):
            return True
        return not preferences.show_private and (sensitive_field(key) or sensitive_field(leaf))

    hidden = (*secrets, "" if preferences.show_serial else serial,
              "" if preferences.show_private or device_name == serial else device_name)
    return sanitize(payload, tuple(item for item in hidden if item), protect=protect,
                    show_private=preferences.show_private, show_serial=preferences.show_serial, scrub_numeric=True)


class Subscription:
    def __init__(self, channel):
        self.channel = channel
        self.frames = deque()
        self.closed = False


class ExportHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get_all("Host") or []
        if len(host) != 1 or host[0] not in self.server.host_headers or self.headers.get("Origin") is not None or (
            self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")
        ):
            self.send_error(403)
            return
        if self.path == SNAPSHOT_PATH:
            snapshot = self.server.export.snapshot()
            if snapshot is None:
                self.send_error(503)
                return
            frame = encoded(snapshot)
            self._headers("application/json", length=len(frame))
            self.wfile.write(frame)
            return
        channel = STREAMS.get(self.path)
        if channel is None:
            self.send_error(404)
            return
        with self.server.export.subscribe(channel) as (status, subscription):
            if subscription is None:
                self.send_error(status)
                return
            self._headers("text/event-stream; charset=utf-8")
            while (frame := self.server.export.next_frame(subscription)) is not None:
                self.wfile.write(b"data: " + frame + b"\n\n" if frame else b": keepalive\n\n")

    def _headers(self, content_type, *, length=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def log_message(self, *args):
        pass


class ExportHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")
    request_queue_size = MAX_CLIENTS

    def __init__(self, port, export):
        self.export = export
        self._clients = set()
        self._client_lock = threading.Lock()
        super().__init__((LOOPBACK, port), ExportHandler)
        port = self.server_port
        self.host_headers = (f"{LOOPBACK}:{port}", f"localhost:{port}")
        if port == 80:
            self.host_headers += (LOOPBACK, "localhost")

    def process_request(self, request, client_address):
        with self._client_lock:
            if len(self._clients) >= MAX_CLIENTS:
                super().shutdown_request(request)
                return
            self._clients.add(request)
        try:
            request.settimeout(2)
            super().process_request(request, client_address)
        except (OSError, RuntimeError):
            self.shutdown_request(request)

    def shutdown_request(self, request):
        super().shutdown_request(request)
        with self._client_lock:
            self._clients.discard(request)

    def disconnect_clients(self):
        with self._client_lock:
            for request in self._clients:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    continue

    def handle_error(self, request, client_address):
        logger.debug("A telemetry export connection ended with an error")


class TelemetryExport:
    def __init__(self, preferences, clock=time.monotonic):
        self.preferences = preferences
        self.clock = clock
        self.error = False
        self._server = None
        self._thread = None
        self._condition = threading.Condition()
        self._subscriptions = set()
        self._snapshot = None
        self._deadlines = {}
        self._epoch = 0
        self._serial = ""
        self._name = ""

    @property
    def running(self):
        return self._server is not None

    def configure(self, preferences, *, active, serial, device_name, reset=False):
        same_port = preferences.port == self.preferences.port
        if self.running and (not preferences.enabled or not active or not same_port):
            self.stop()
        with self._condition:
            changed = reset or preferences != self.preferences or serial != self._serial
            self.preferences = preferences
            self._serial, self._name = serial, device_name
            if changed:
                self._clear()
        if changed and self.running:
            self._server.disconnect_clients()
        if not (preferences.enabled and active):
            self.error = False
            return
        if self.running:
            return
        try:
            server = ExportHTTPServer(preferences.port, self)
        except OSError:
            self.error = True
            logger.warning("Could not listen on the configured telemetry export port")
            return
        self.error = False
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def stop(self):
        with self._condition:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            self._clear()
        if server is not None:
            server.disconnect_clients()
            server.shutdown()
            server.server_close()
            thread.join()

    def _clear(self):
        self._epoch += 1
        self._snapshot = None
        self._deadlines = {}
        for item in self._subscriptions:
            item.closed = True
            item.frames.clear()
        self._subscriptions.clear()
        self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            if not self.running or self._snapshot is None:
                return None
            now = self.clock()
            values = {key: value for key, value in self._snapshot["values"].items() if self._deadlines[key] > now}
            return {**self._snapshot, "values": values}

    def publish(self, state, source, values, deadlines, device_name, *, authorized=True):
        with self._condition:
            if not self.running:
                return
            self._name = device_name
            metadata = {}
            if authorized and self._serial and self.preferences.show_serial:
                metadata["serial_number"] = self._serial
            if device_name and self.preferences.show_private:
                metadata["device_name"] = device_name
            self._snapshot = {
                "version": 1, "timestamp": utc_now(), "state": state, "source": source, "values": values,
                **(sanitize_export(metadata, self.preferences, serial=self._serial) if metadata else {}),
            }
            self._deadlines = deadlines
            self._broadcast("telemetry", encoded(self.snapshot()))

    def invalidate(self, available, state):
        with self._condition:
            if self._snapshot is not None:
                self._snapshot["values"] = {key: value for key, value in self._snapshot["values"].items() if key in available}
                self._snapshot["state"] = state

    def emit(self, channel, payload, *, secrets=(), current=None):
        if not self.running:
            return
        with self._condition:
            if not self.running or not getattr(self.preferences, channel) or (current is not None and not current()):
                return
            recipients = tuple(item for item in self._subscriptions if item.channel == channel and not item.closed)
            if not recipients:
                return
            policy, epoch, serial, name = self.preferences, self._epoch, self._serial, self._name
        timestamp = utc_now()
        if channel == "raw":
            payload = {"source": payload["source"], "payload": sanitize_export(
                payload["payload"], policy, serial=serial, device_name=name, secrets=secrets,
            )}
            if policy.show_serial and serial:
                payload["serial_number"] = sanitize_export(serial, policy, secrets=secrets)
        else:
            payload = {key: sanitize_export(value, policy, serial=serial, device_name=name, secrets=secrets)
                       for key, value in payload.items()}
        try:
            frame = encoded({"version": 1, "timestamp": timestamp, **payload})
        except (ValueError, RecursionError):
            return
        if len(frame) > MAX_FRAME:
            return
        with self._condition:
            if epoch == self._epoch and (current is None or current()):
                self._broadcast(channel, frame, recipients)

    @contextmanager
    def subscribe(self, channel):
        with self._condition:
            item = None
            status = 200
            if not self.running:
                status = 503
            elif channel != "telemetry" and not getattr(self.preferences, channel):
                status = 404
            elif len(self._subscriptions) >= MAX_STREAMS:
                status = 503
            else:
                item = Subscription(channel)
                self._subscriptions.add(item)
                if channel == "telemetry" and (snapshot := self.snapshot()) is not None:
                    item.frames.append(encoded(snapshot))
        try:
            yield status, item
        finally:
            if item is not None:
                with self._condition:
                    self._subscriptions.discard(item)

    def next_frame(self, item):
        with self._condition:
            if not self._condition.wait_for(lambda: item.closed or item.frames, timeout=15):
                return b""
            if item.closed:
                return None
            return item.frames.popleft()

    def _broadcast(self, channel, frame, recipients=None):
        for item in self._subscriptions if recipients is None else recipients:
            if item.closed or item.channel != channel or item not in self._subscriptions:
                continue
            if channel == "telemetry":
                item.frames.clear()
            if len(item.frames) == MAX_PENDING:
                item.frames.clear()
                item.closed = True
            else:
                item.frames.append(frame)
        self._condition.notify_all()

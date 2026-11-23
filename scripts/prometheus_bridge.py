import argparse
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import re


LOOPBACK = "127.0.0.1"
PREFIX = "ecoflow_monitor_"
MAX_SNAPSHOT = 65536
METRICS = {
    "battery_level": "battery_percent",
    "input_power": "input_power_watts",
    "output_power": "output_power_watts",
    "minimum_discharge_level": "minimum_discharge_percent",
    "maximum_charge_level": "maximum_charge_percent",
    "capacity_wh": "capacity_watt_hours",
    "remaining_time_seconds": "remaining_time_seconds",
    "device_discharge_seconds": "device_discharge_seconds",
    "device_charge_seconds": "device_charge_seconds",
    "charging_state": "charging_state",
    "ac_enabled": "ac_output_enabled",
    "dc_enabled": "dc_output_enabled",
}
PORT = re.compile(r"(?:ac_input|pv_input|ac_output|dc_output|usb_c[123]|usb_a[12]|anderson_(?:[1-9]|[1-5][0-9]|6[0-4]))")


def read_snapshot(port):
    client = HTTPConnection(LOOPBACK, port, timeout=2)
    try:
        client.request("GET", "/v1/telemetry")
        response = client.getresponse()
        if response.status != 200:
            return None
        data = response.read(MAX_SNAPSHOT + 1)
        if len(data) > MAX_SNAPSHOT:
            return None
        snapshot = json.loads(data)
        if isinstance(snapshot, dict) and snapshot.get("version") == 1 and isinstance(snapshot.get("values"), dict):
            return snapshot
    except (OSError, HTTPException, ValueError, RecursionError):
        return None
    finally:
        client.close()
    return None


def label(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"').replace("\r", "")


def numeric(value):
    return type(value) in (int, bool) or type(value) is float and math.isfinite(value)


def render_metrics(snapshot):
    lines = []

    def gauge(name, samples):
        name = PREFIX + name
        lines.append(f"# TYPE {name} gauge")
        lines.extend(f"{name}{labels} {int(value) if type(value) is bool else value}" for labels, value in samples)

    gauge("up", [("", snapshot is not None)])
    if snapshot is not None:
        gauge("telemetry_live", [("", snapshot.get("state") == "connected")])
        values = snapshot["values"]
        for key, name in METRICS.items():
            if numeric(value := values.get(key)):
                gauge(name, [("", value)])
        ports = [(f'{{port="{key}"}}', value) for key, value in sorted(values.items()) if PORT.fullmatch(key) and numeric(value)]
        if ports:
            gauge("port_watts", ports)
        metadata = [f'{key}="{label(value)}"' for key in ("serial_number", "device_name")
                    if isinstance(value := snapshot.get(key), str) and value and value != "[REDACTED]"]
        if metadata:
            gauge("device_info", [("{" + ",".join(metadata) + "}", 1)])
    return ("\n".join(lines) + "\n").encode("utf-8")


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_port
        host = self.headers.get_all("Host") or []
        if len(host) != 1 or host[0] not in (f"{LOOPBACK}:{port}", f"localhost:{port}") or self.headers.get("Origin") is not None:
            self.send_error(403)
            return
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = render_metrics(read_snapshot(self.server.monitor_port))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Bridge(HTTPServer):
    allow_reuse_address = False

    def __init__(self, port, monitor_port):
        self.monitor_port = monitor_port
        super().__init__((LOOPBACK, port), MetricsHandler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(2)
        return request, address


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return port


def main():
    parser = argparse.ArgumentParser(description="Expose the local EcoFlow Monitor telemetry snapshot to Prometheus.")
    parser.add_argument("--monitor-port", type=port_number, default=9469)
    parser.add_argument("--port", type=port_number, default=9470)
    args = parser.parse_args()
    if args.port == args.monitor_port:
        parser.error("The bridge port must differ from the Monitor port")
    try:
        with Bridge(args.port, args.monitor_port) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError:
        parser.exit(2, f"Could not listen on {LOOPBACK}:{args.port}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

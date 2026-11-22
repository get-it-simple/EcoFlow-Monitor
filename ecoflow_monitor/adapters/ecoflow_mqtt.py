from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
import json
import re
from select import select
import socket
import ssl
from threading import Event, Lock
from typing import Callable

from ecoflow_monitor.device_profiles import SENSORS, baseline_profile
from ecoflow_monitor.polling import TelemetryPatch


MAX_REPORT_BYTES = 64 * 1024


class MqttError(Exception):
    pass


class MqttCancellation(Event):
    def __init__(self):
        super().__init__()
        self._lock = Lock()
        self._sender = None

    def set(self):
        with self._lock:
            if self.is_set():
                return
            super().set()
            if self._sender is not None:
                try:
                    self._sender.send(b"\0")
                except OSError:
                    pass

    @contextmanager
    def wakeup_socket(self):
        reader, sender = socket.socketpair()
        with reader, sender:
            sender.setblocking(False)
            with self._lock:
                self._sender = sender
            try:
                yield reader
            finally:
                with self._lock:
                    self._sender = None


@dataclass(frozen=True)
class BrokerAccess:
    host: str
    port: int
    account: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def from_response(cls, data: object) -> "BrokerAccess":
        if not isinstance(data, dict) or data.get("protocol") != "mqtts":
            raise MqttError("invalid_certification")
        host, port = data.get("url"), data.get("port")
        account, password = data.get("certificateAccount"), data.get("certificatePassword")
        if not isinstance(host, str) or not re.fullmatch(r"(?:[a-z0-9-]+\.)+ecoflow\.com", host, re.IGNORECASE):
            raise MqttError("invalid_broker")
        if isinstance(port, str) and re.fullmatch(r"[0-9]{1,5}", port):
            port = int(port)
        if type(port) is not int or not 1 <= port <= 65535:
            raise MqttError("invalid_broker_port")
        if not valid_topic_segment(account) or not isinstance(password, str) or not password or "\0" in password:
            raise MqttError("invalid_broker_credentials")
        return cls(host, port, account, password)


def valid_topic_segment(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


@lru_cache(maxsize=1)
def mqtt_profile():
    profile = baseline_profile()
    fields = tuple((sensor, replace(mapping, sources=(("powGet12vList", "powGet12vItem"),))
                    if sensor == "anderson" else mapping) for sensor, mapping in profile.fields)
    return replace(profile, fields=fields)


def decode_report(payload: bytes) -> TelemetryPatch:
    if len(payload) > MAX_REPORT_BYTES:
        return TelemetryPatch((), ())
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError):
        return TelemetryPatch((), ())
    if not isinstance(data, dict):
        return TelemetryPatch((), ())
    values, _, invalid = mqtt_profile().decode(data, SENSORS)
    return TelemetryPatch(tuple(key for key, _ in values) + invalid, values, invalid=invalid)


def stream_telemetry(access: BrokerAccess, serial: str, cancelled: MqttCancellation, receive: Callable[[TelemetryPatch], None]) -> None:
    from paho.mqtt import client as mqtt

    if not valid_topic_segment(serial):
        raise MqttError("invalid_device_topic")
    if cancelled.is_set():
        return
    topic = f"/open/{access.account}/{serial}/quota"
    failure = None
    subscribed = False

    def connected(client, userdata, flags, reason, properties):
        nonlocal failure
        if reason.is_failure:
            failure = "connection_rejected"
        elif not cancelled.is_set() and client.subscribe(topic, qos=0)[0] != mqtt.MQTT_ERR_SUCCESS:
            failure = "subscription_failed"

    def acknowledged(client, userdata, mid, reasons, properties):
        nonlocal failure, subscribed
        subscribed = len(reasons) == 1 and not reasons[0].is_failure
        if not subscribed:
            failure = "subscription_rejected"

    def message(client, userdata, report):
        if subscribed and not cancelled.is_set() and report.topic == topic and not report.retain:
            patch = decode_report(report.payload)
            if patch.requested:
                receive(patch)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.connect_timeout = 5.0
    client.tls_set_context(ssl.create_default_context())
    client.username_pw_set(access.account, access.password)
    client.on_connect = connected
    client.on_subscribe = acknowledged
    client.on_message = message
    try:
        if client.connect(access.host, access.port, keepalive=30) != mqtt.MQTT_ERR_SUCCESS:
            raise MqttError("connection_failed")
        with cancelled.wakeup_socket() as wakeup:
            while not cancelled.is_set():
                broker = client.socket()
                if broker is None:
                    raise MqttError("stream_disconnected")
                pending = isinstance(broker, ssl.SSLSocket) and broker.pending() > 0
                readable, writable, _ = select([broker, wakeup], [broker] if client.want_write() else [], [], 0 if pending else 5)
                if wakeup in readable or cancelled.is_set():
                    break
                code = client.loop_read() if broker in readable or pending else mqtt.MQTT_ERR_SUCCESS
                if code == mqtt.MQTT_ERR_SUCCESS and broker in writable:
                    code = client.loop_write()
                if code == mqtt.MQTT_ERR_SUCCESS:
                    code = client.loop_misc()
                if failure:
                    raise MqttError(failure)
                if code != mqtt.MQTT_ERR_SUCCESS:
                    raise MqttError("stream_disconnected")
    except OSError:
        raise MqttError("transport_failure") from None
    finally:
        client.disconnect()

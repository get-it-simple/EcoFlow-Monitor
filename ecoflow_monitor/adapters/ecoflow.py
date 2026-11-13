from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import math
import logging
import secrets
import time
import uuid
from typing import Any

import httpx

from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.diagnostics import event


HOST = "https://api.ecoflow.com"
PORT_KEYS = {
    "ac_input": "powGetAcIn", "pv_input": "powGetPv", "ac_output": "powGetAcOut",
    "dc_output": "powGet12v", "usb_c1": "powGetTypec1", "usb_c2": "powGetTypec2",
    "usb_c3": "powGetTypec3", "usb_a1": "powGetQcusb1", "usb_a2": "powGetQcusb2",
}
QUOTAS = (
    "powInSumW", "powOutSumW", "cmsBattSoc", "cmsMinDsgSoc", "cmsMaxChgSoc",
    *PORT_KEYS.values(), "powGet12vList.powGet12vItem", "flowInfoAcOut", "flowInfo12v",
    "cmsChgDsgState", "cmsDsgRemTime", "cmsChgRemTime", "energyBackupEn",
    "backupReverseSoc", "enBeep", "xboostEn", "bypassOutDisable",
)


@dataclass(frozen=True)
class Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class Device:
    serial: str
    name: str
    online: bool


class EcoFlowError(Exception):
    def __init__(self, state: ConnectionState, retry_after: float | None = None) -> None:
        super().__init__(state.value)
        self.state = state
        self.retry_after = retry_after


def flatten_parameters(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in flatten_parameters(child, f"{prefix}.{key}" if prefix else key)]
    if isinstance(value, (tuple, list)):
        return [item for index, child in enumerate(value) for item in flatten_parameters(child, prefix + "[" + str(index) + "]")]
    if value is None:
        raise ValueError("Null request parameters are not supported")
    if isinstance(value, bool):
        return [(prefix, "true" if value else "false")]
    return [(prefix, str(value))]


def signed_headers(parameters: dict, credentials: Credentials, nonce: int, timestamp: int) -> dict[str, str]:
    canonical = "&".join(f"{key}={value}" for key, value in sorted(flatten_parameters(parameters)))
    authorization = f"accessKey={credentials.access_key}&nonce={nonce}&timestamp={timestamp}"
    message = f"{canonical}&{authorization}" if canonical else authorization
    signature = hmac.new(credentials.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"accessKey": credentials.access_key, "nonce": str(nonce), "timestamp": str(timestamp), "sign": signature}


def metric(value: Any, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 and (maximum is None or number <= maximum) else None


def normalize_telemetry(data: dict, utc: float, monotonic: float) -> Telemetry:
    if not {"powInSumW", "powOutSumW", "cmsBattSoc"} <= data.keys():
        event("telemetry_rejected", level=logging.DEBUG, reason="missing_required_fields",
              fields=sorted({"powInSumW", "powOutSumW", "cmsBattSoc"} - data.keys()))
        raise EcoFlowError(ConnectionState.UNSUPPORTED)
    core = [metric(data.get("powInSumW")), metric(data.get("powOutSumW")), metric(data.get("cmsBattSoc"), 100)]
    if any(value is None for value in core):
        event("telemetry_rejected", level=logging.DEBUG, reason="invalid_required_fields",
              fields=[key for key, value in zip(("powInSumW", "powOutSumW", "cmsBattSoc"), core) if value is None])
        raise EcoFlowError(ConnectionState.API_ERROR)
    ports = [(name, value) for name, key in PORT_KEYS.items() if (value := metric(data.get(key))) is not None]
    anderson = data.get("powGet12vList.powGet12vItem")
    if isinstance(anderson, list):
        ports.extend((f"anderson_{index + 1}", value) for index, item in enumerate(anderson) if (value := metric(item)) is not None)
    state = metric(data.get("cmsChgDsgState"), 2)
    snapshot = Telemetry(
        input_w=core[0], output_w=core[1], soc_pct=core[2], received_utc=utc,
        received_monotonic=monotonic, min_soc=metric(data.get("cmsMinDsgSoc"), 30),
        max_soc=metric(data.get("cmsMaxChgSoc"), 100), ports=tuple(ports),
        device_discharge_minutes=metric(data.get("cmsDsgRemTime")),
        device_charge_minutes=metric(data.get("cmsChgRemTime")),
        charging_state=int(state) if state is not None and state.is_integer() else None,
        ac_enabled=outlet_state(data.get("flowInfoAcOut")),
        dc_enabled=outlet_state(data.get("flowInfo12v")),
    )
    ranges = {"cmsMinDsgSoc": 30, "cmsMaxChgSoc": 100, "cmsChgDsgState": 2}
    numeric_fields = {*PORT_KEYS.values(), *ranges, "cmsDsgRemTime", "cmsChgRemTime", "flowInfoAcOut", "flowInfo12v"}
    invalid = [key for key in sorted(numeric_fields & data.keys()) if metric(data[key], ranges.get(key)) is None
               or (key in {"cmsChgDsgState", "flowInfoAcOut", "flowInfo12v"} and not float(data[key]).is_integer())]
    anderson = data.get("powGet12vList.powGet12vItem")
    if "powGet12vList.powGet12vItem" in data and (not isinstance(anderson, list) or any(metric(item) is None for item in anderson)):
        invalid.append("powGet12vList.powGet12vItem")
    event("telemetry_normalized", level=logging.DEBUG, telemetry=asdict(snapshot),
          missing_fields=[key for key in QUOTAS if key not in data], invalid_fields=invalid,
          invalid_reason="invalid_type_range_or_value" if invalid else None)
    return snapshot


def outlet_state(value: Any) -> bool | None:
    number = metric(value)
    return number != 4 if number is not None and number.is_integer() else None


class EcoFlowClient:
    def __init__(self, credentials: Credentials, transport: httpx.BaseTransport | None = None) -> None:
        self._credentials = credentials
        self._transport = transport

    def _request(self, method: str, path: str, parameters: dict) -> Any:
        operation = uuid.uuid4().hex[:12]
        started = time.monotonic()
        hidden = (self._credentials.access_key, self._credentials.secret_key, str(parameters.get("sn", "")))
        event("api_request_started", operation=operation, method=method, endpoint=path)
        event("api_request_parameters", level=logging.DEBUG, sensitive=hidden, operation=operation, parameters=parameters)
        headers = signed_headers(parameters, self._credentials, secrets.randbelow(900000) + 100000, int(time.time() * 1000))
        if method == "POST":
            headers["Content-Type"] = "application/json;charset=UTF-8"
        try:
            with httpx.Client(timeout=10, follow_redirects=False, transport=self._transport) as client:
                response = client.request(
                    method, HOST + path, headers=headers,
                    params=parameters if method == "GET" else None,
                    json=parameters if method == "POST" else None,
                )
        except httpx.HTTPError as error:
            event("api_transport_failed", level=logging.WARNING, operation=operation, exception_type=type(error).__name__)
            event("api_request_timing", level=logging.DEBUG, operation=operation,
                  elapsed_ms=round((time.monotonic() - started) * 1000, 2))
            raise EcoFlowError(ConnectionState.API_ERROR) from None
        event("api_request_completed", operation=operation, http_status=response.status_code)
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            payload = None
        hidden = (*hidden, headers["sign"], headers["nonce"])
        event("api_response", level=logging.DEBUG, sensitive=hidden, operation=operation,
              http_status=response.status_code, elapsed_ms=round((time.monotonic() - started) * 1000, 2),
              body_bytes=len(response.content), retry_after_seconds=metric(response.headers.get("Retry-After")),
              payload=payload, body_format="json" if payload is not None else "non_json_or_null")
        if response.status_code in (401, 403):
            raise EcoFlowError(ConnectionState.AUTH_ERROR)
        if response.status_code >= 400:
            delay = metric(response.headers.get("Retry-After"))
            raise EcoFlowError(ConnectionState.API_ERROR, delay)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise EcoFlowError(ConnectionState.API_ERROR)
        return payload.get("data")

    def devices(self) -> tuple[Device, ...]:
        data = self._request("GET", "/iot-open/sign/device/list", {})
        if not isinstance(data, list):
            event("device_list_rejected", level=logging.DEBUG, reason="expected_array")
            raise EcoFlowError(ConnectionState.API_ERROR)
        return tuple(
            Device(item["sn"], str(item.get("deviceName") or item["sn"]), item.get("online") == 1)
            for item in data if isinstance(item, dict) and isinstance(item.get("sn"), str) and item["sn"]
        )

    def read(self, serial: str) -> Telemetry:
        devices = self.devices()
        device = next((device for device in devices if device.serial == serial), None)
        if device is None:
            event("device_unavailable", reason="not_in_account")
            raise EcoFlowError(ConnectionState.UNSUPPORTED)
        if not device.online:
            event("device_unavailable", reason="offline")
            raise EcoFlowError(ConnectionState.DEVICE_OFFLINE)
        data = self._request("POST", "/iot-open/sign/device/quota", {"sn": serial, "params": {"quotas": list(QUOTAS)}})
        if not isinstance(data, dict):
            event("telemetry_rejected", level=logging.DEBUG, reason="expected_object")
            raise EcoFlowError(ConnectionState.API_ERROR)
        return normalize_telemetry(data, time.time(), time.monotonic())

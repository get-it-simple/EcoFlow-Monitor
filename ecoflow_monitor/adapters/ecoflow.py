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
from ecoflow_monitor.device_profiles import CORE, PORTS, SENSORS, DeviceProfile, baseline_profile
from ecoflow_monitor.polling import SENSOR_FIELDS, TelemetryPatch
from ecoflow_monitor.polling import PORT_KEYS as PORT_KEYS
from ecoflow_monitor.system_activity import ActivityCancelled


HOST = "https://api.ecoflow.com"
QUOTAS = baseline_profile().quotas(SENSORS)


@dataclass(frozen=True)
class Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class Device:
    serial: str
    name: str
    online: bool
    model_id: str = ""


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


def metric(value: Any, maximum: float | None = None, signed: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and (signed or number >= 0) and (maximum is None or number <= maximum) else None


def normalize_telemetry(data: dict, utc: float, monotonic: float, profile: DeviceProfile | None = None) -> Telemetry:
    profile = profile or baseline_profile()
    patch = normalize_partial(data, SENSORS, profile)
    if CORE & set(patch.missing):
        raise EcoFlowError(ConnectionState.UNSUPPORTED)
    if CORE & set(patch.invalid):
        raise EcoFlowError(ConnectionState.API_ERROR)
    values = dict(patch.values)
    ports = [(key, values[key]) for key in PORTS if key in values]
    ports.extend((f"anderson_{index + 1}", value) for index, value in enumerate(values.get("anderson", ())))
    snapshot = Telemetry(
        input_w=values["input_power"], output_w=values["output_power"], soc_pct=values["battery_level"], received_utc=utc,
        received_monotonic=monotonic, min_soc=values.get("minimum_discharge_level"),
        max_soc=values.get("maximum_charge_level"), ports=tuple(ports),
        device_discharge_minutes=values.get("device_discharge_minutes"), device_charge_minutes=values.get("device_charge_minutes"),
        charging_state=values.get("charging_state"), ac_enabled=values.get("ac_enabled"), dc_enabled=values.get("dc_enabled"),
    )
    event("telemetry_normalized", level=logging.DEBUG, telemetry=asdict(snapshot),
          profile_id=profile.id, profile_source=profile.source, profile_revision=profile.revision,
          missing_fields=list(profile.quotas(patch.missing)), invalid_fields=list(profile.quotas(patch.invalid)),
          missing_sensors=list(patch.missing), invalid_sensors=list(patch.invalid))
    return snapshot


def outlet_state(value: Any) -> bool | None:
    mapping = dict(baseline_profile().fields)["ac_enabled"]
    source = mapping.sources[0][0]
    status, result = mapping.decode({source: value}, "ac_enabled")
    return result if status == "valid" else None


def normalize_partial(data: dict, quotas: tuple[str, ...], profile: DeviceProfile | None = None) -> TelemetryPatch:
    profile = profile or baseline_profile()
    requested = tuple(key for key in SENSORS if key in quotas or profile is baseline_profile() and SENSOR_FIELDS[key] in quotas)
    values, missing, invalid = profile.decode(data, requested)
    event("telemetry_partial_normalized", level=logging.DEBUG, requested_fields=list(requested),
          profile_id=profile.id, profile_source=profile.source, profile_revision=profile.revision,
          missing_fields=list(missing), invalid_fields=list(invalid),
          telemetry=dict(values))
    return TelemetryPatch(requested, tuple(values), tuple(missing), tuple(invalid))


class EcoFlowClient:
    def __init__(self, credentials: Credentials, transport: httpx.BaseTransport | None = None) -> None:
        self._credentials = credentials
        self._transport = transport
        self.catalog = None
        self.diagnostic_capture = None
        self.cancelled = None
        self.profile = baseline_profile()

    def _request(self, method: str, path: str, parameters: dict) -> Any:
        if self.cancelled is not None and self.cancelled.is_set():
            raise ActivityCancelled()
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
            if self.diagnostic_capture is not None:
                self.diagnostic_capture("device_diagnostic_transport_failure", endpoint=path, exception_type=type(error).__name__)
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
        if self.diagnostic_capture is not None:
            self.diagnostic_capture("device_diagnostic_response", sensitive=hidden, endpoint=path,
                                    http_status=response.status_code, payload=payload,
                                    body_format="json" if payload is not None else "non_json_or_null")
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
            Device(item["sn"], str(item.get("deviceName") or item["sn"]), item.get("online") == 1,
                   str(item["modelId"]) if type(item.get("modelId")) in (str, int) else "")
            for item in data if isinstance(item, dict) and isinstance(item.get("sn"), str) and item["sn"]
        )

    def read(self, serial: str) -> Telemetry:
        self._check_device(serial)
        if self.profile is None or not self.profile.usable:
            raise EcoFlowError(ConnectionState.UNSUPPORTED)
        data = self._read_profile(serial, SENSORS)
        if not isinstance(data, dict):
            event("telemetry_rejected", level=logging.DEBUG, reason="expected_object")
            raise EcoFlowError(ConnectionState.API_ERROR)
        return normalize_telemetry(data, time.time(), time.monotonic(), self.profile)

    def _read_profile(self, serial, sensors):
        profile = self.profile
        parameters = {"sn": serial}
        if profile.method == "POST":
            parameters["params"] = {"quotas": list(profile.quotas(sensors))}
        event("device_profile_read", profile_id=profile.id, profile_source=profile.source,
              profile_revision=profile.revision, endpoint=profile.endpoint, method=profile.method)
        return self._request(profile.method, profile.endpoint, parameters)

    def diagnose(self, serial: str, capture) -> tuple[str, ...]:
        self.diagnostic_capture = capture
        failures = []
        try:
            operations = [("devices", "GET", "/iot-open/sign/device/list", {})]
            if self.profile is not None:
                profile = self.profile
                capture("device_diagnostic_profile", profile_id=profile.id, profile_source=profile.source, profile_revision=profile.revision)
                parameters = {"sn": serial}
                if profile.method == "POST":
                    parameters["params"] = {"quotas": list(profile.quotas(SENSORS))}
                operations.append(("selected_parameters" if profile.method == "POST" else "all_parameters", profile.method, profile.endpoint, parameters))
            else:
                capture("device_diagnostic_profile_missing", reason="select_profile_before_model_requests")
            for label, method, endpoint, parameters in operations:
                try:
                    data = self._request(method, endpoint, parameters)
                    if label == "devices":
                        if not isinstance(data, list):
                            raise EcoFlowError(ConnectionState.API_ERROR)
                    elif not isinstance(data, dict):
                        raise EcoFlowError(ConnectionState.API_ERROR)
                    else:
                        patch = normalize_partial(data, SENSORS, self.profile)
                        capture("device_diagnostic_validation", operation=label, missing_fields=list(patch.missing),
                                invalid_fields=list(patch.invalid), empty=not data)
                        if not data or patch.invalid or not CORE <= dict(patch.values).keys():
                            failures.append(label)
                except EcoFlowError as error:
                    failures.append(label)
                    capture("device_diagnostic_failure", operation=label, state=error.state.value)
                    if error.state == ConnectionState.AUTH_ERROR:
                        break
        finally:
            self.diagnostic_capture = None
        return tuple(failures)

    def read_quotas(self, serial: str, select_quotas, *, verify_model: bool = False) -> TelemetryPatch:
        self._check_device(serial)
        if self.profile is None or not self.profile.usable:
            raise EcoFlowError(ConnectionState.UNSUPPORTED)
        selected = tuple(select_quotas())
        if not selected:
            return TelemetryPatch((), ())
        legacy = {value: key for key, value in SENSOR_FIELDS.items()} if self.profile is baseline_profile() else {}
        sensors = tuple(legacy.get(key, key) for key in selected)
        if set(sensors) - set(SENSORS):
            raise ValueError("Unknown telemetry quota")
        supported = tuple(key for key in sensors if key in dict(self.profile.fields))
        data = self._read_profile(serial, supported) if supported else {}
        if not isinstance(data, dict):
            raise EcoFlowError(ConnectionState.API_ERROR)
        patch = normalize_partial(data, sensors, self.profile)
        if verify_model:
            if CORE & set(patch.missing):
                raise EcoFlowError(ConnectionState.UNSUPPORTED)
            if not CORE <= dict(patch.values).keys():
                raise EcoFlowError(ConnectionState.API_ERROR)
        return patch

    def _check_device(self, serial: str) -> None:
        devices = self.devices() if self.catalog is None else self.catalog
        device = next((device for device in devices if device.serial == serial), None)
        if device is None:
            event("device_unavailable", reason="not_in_account")
            raise EcoFlowError(ConnectionState.UNSUPPORTED)
        if not device.online and self.catalog is None:
            event("device_unavailable", reason="offline")
            raise EcoFlowError(ConnectionState.DEVICE_OFFLINE)

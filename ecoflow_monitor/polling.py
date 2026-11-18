from dataclasses import dataclass, field, replace
import math
import threading

from ecoflow_monitor.domain import Telemetry
from ecoflow_monitor.device_profiles import CORE, PORTS as PORT_NAMES, SENSORS, baseline_profile


SENSOR_FIELDS = {key: mapping.quotas[0] for key, mapping in baseline_profile().fields}
PORT_KEYS = {key: SENSOR_FIELDS[key] for key in PORT_NAMES}
LIMITS = frozenset(("minimum_discharge_level", "maximum_charge_level"))
PORTS = frozenset((*PORT_KEYS, "anderson"))
DASHBOARD_SENSORS = frozenset(SENSOR_FIELDS) - {"pv_input", "device_discharge_minutes", "device_charge_minutes"}
ATTRIBUTES = {
    "input_power": "input_w", "output_power": "output_w", "battery_level": "soc_pct",
    "minimum_discharge_level": "min_soc", "maximum_charge_level": "max_soc",
    "ac_enabled": "ac_enabled", "dc_enabled": "dc_enabled", "charging_state": "charging_state",
    "device_discharge_minutes": "device_discharge_minutes", "device_charge_minutes": "device_charge_minutes",
}


def dependencies(key: str) -> frozenset[str]:
    if key == "estimated_runtime":
        return CORE | LIMITS | {"ac_input"}
    if key.startswith("anderson_"):
        return frozenset(("anderson",))
    return frozenset((key,)) if key in SENSOR_FIELDS else frozenset()


def sensor_group(key: str) -> str:
    return "limits" if key in LIMITS else "ports" if key in PORTS else "primary"


@dataclass(frozen=True)
class PollingGroup:
    interval_seconds: int = 30
    stale_after_seconds: int = 90

    def validate(self) -> None:
        if type(self.interval_seconds) is not int or not 5 <= self.interval_seconds <= 3600:
            raise ValueError("polling_interval_error")
        if type(self.stale_after_seconds) is not int or not self.interval_seconds <= self.stale_after_seconds <= 86400:
            raise ValueError("polling_stale_error")


@dataclass(frozen=True)
class PollingPreferences:
    primary: PollingGroup = field(default_factory=PollingGroup)
    ports: PollingGroup = field(default_factory=lambda: PollingGroup(60, 180))
    limits: PollingGroup = field(default_factory=lambda: PollingGroup(300, 900))

    def validate(self) -> None:
        for group in (self.primary, self.ports, self.limits):
            if not isinstance(group, PollingGroup):
                raise ValueError("polling_interval_error")
            group.validate()

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - {"primary", "ports", "limits"}:
            raise ValueError("polling_interval_error")
        defaults = cls()
        values = {}
        for name in ("primary", "ports", "limits"):
            group = data.get(name, {})
            if not isinstance(group, dict) or set(group) - {"interval_seconds", "stale_after_seconds"}:
                raise ValueError("polling_interval_error")
            values[name] = replace(getattr(defaults, name), **group)
        result = cls(**values)
        result.validate()
        return result


@dataclass(frozen=True)
class TelemetryPatch:
    requested: tuple[str, ...]
    values: tuple[tuple[str, object], ...]
    missing: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()


@dataclass(frozen=True)
class SensorSample:
    value: object = None
    received_utc: float | None = None
    received_monotonic: float | None = None
    attempted_monotonic: float = -math.inf
    status: str = "missing"


class PollingState:
    def __init__(self, preferences: PollingPreferences):
        self._lock = threading.RLock()
        self.preferences = preferences
        self.samples: dict[str, SensorSample] = {}
        self.visible = frozenset()
        self.required = frozenset()
        self.supplemental = frozenset()
        self.in_flight = frozenset()
        self.waiting = set()
        self.resumed = set()
        self.verified = False
        self.revision = 0

    def configure(self, preferences: PollingPreferences) -> None:
        with self._lock:
            self.preferences = preferences

    def set_demand(self, visible, required, now: float) -> bool:
        with self._lock:
            visible, required = frozenset(visible), frozenset(required)
            if (visible, required) == (self.visible, self.required):
                return False
            for key in visible - self.visible:
                sample = self.samples.get(key)
                age = math.inf if sample is None or sample.received_monotonic is None else now - sample.received_monotonic
                group = getattr(self.preferences, sensor_group(key))
                if age >= group.stale_after_seconds:
                    self.waiting.add(key)
                elif age >= group.interval_seconds:
                    self.resumed.add(key)
            self.waiting.intersection_update(visible)
            self.resumed.intersection_update(visible)
            self.visible, self.required = visible, required
            self.revision += 1
            return True

    def due(self, now: float) -> frozenset[str]:
        with self._lock:
            needed = self.required | (CORE if self.required and not self.verified else frozenset())
            return frozenset(key for key in needed if key not in self.in_flight and
                             now - self.samples.get(key, SensorSample()).attempted_monotonic >=
                             getattr(self.preferences, sensor_group(key)).interval_seconds)

    def begin(self, now: float) -> tuple[str, ...]:
        with self._lock:
            keys = self.due(now)
            if keys:
                keys |= frozenset(key for key in self.supplemental if key not in self.in_flight
                                  and now - self.samples.get(key, SensorSample()).attempted_monotonic
                                  >= getattr(self.preferences, sensor_group(key)).interval_seconds)
            self.in_flight = keys
            for key in keys:
                self.samples[key] = replace(self.samples.get(key, SensorSample()), attempted_monotonic=now)
            self.revision += 1
            return tuple(key for key in SENSORS if key in keys)

    def complete(self, patch: TelemetryPatch, utc: float, now: float) -> None:
        with self._lock:
            values = dict(patch.values)
            for key in patch.requested:
                old = self.samples.get(key, SensorSample())
                self.samples[key] = (SensorSample(values[key], utc, now, now, "valid") if key in values else
                                     replace(old, attempted_monotonic=now, status="invalid" if key in patch.invalid else "missing"))
            self.verified = self.verified or CORE <= values.keys()
            self._finish(patch.requested)

    def fail(self, now: float) -> None:
        with self._lock:
            for key in self.in_flight:
                self.samples[key] = replace(self.samples[key], attempted_monotonic=now, status="error")
            self._finish(self.in_flight or self.required)

    def retry(self) -> None:
        with self._lock:
            for key, sample in tuple(self.samples.items()):
                if sample.status == "error":
                    self.samples[key] = replace(sample, attempted_monotonic=-math.inf)

    def abandon(self) -> None:
        with self._lock:
            for key in self.in_flight:
                self.samples[key] = replace(self.samples[key], attempted_monotonic=-math.inf)
            self._finish(self.in_flight)

    def resume(self, now: float) -> None:
        with self._lock:
            for key in self.required:
                sample = self.samples.get(key, SensorSample())
                self.samples[key] = replace(sample, attempted_monotonic=-math.inf)
            self.in_flight = frozenset()
            visible, required = self.visible, self.required
            self.visible = frozenset()
            self.set_demand(visible, required, now)

    def next_due(self, now: float) -> float:
        with self._lock:
            needed = self.required | (CORE if self.required and not self.verified else frozenset())
            return min((self.samples.get(key, SensorSample()).attempted_monotonic +
                        getattr(self.preferences, sensor_group(key)).interval_seconds for key in needed), default=math.inf)

    def _finish(self, keys) -> None:
        self.waiting.difference_update(keys)
        self.resumed.difference_update(keys)
        self.in_flight = frozenset()
        self.revision += 1

    def fresh(self, keys, now: float) -> bool:
        with self._lock:
            return bool(keys) and all((sample := self.samples.get(key)) is not None and sample.status == "valid" and
                                     sample.received_monotonic is not None and
                                     now - sample.received_monotonic < getattr(self.preferences, sensor_group(key)).stale_after_seconds
                                     for key in keys)

    def presentation(self, keys, now: float) -> str:
        with self._lock:
            keys = frozenset(keys)
            for key in keys:
                sample = self.samples.get(key, SensorSample())
                age = math.inf if sample.received_monotonic is None else now - sample.received_monotonic
                if age >= getattr(self.preferences, sensor_group(key)).stale_after_seconds and (key in self.in_flight or key in self.waiting):
                    return "loading"
            if keys & self.resumed & self.in_flight:
                return "refreshing"
            if any(self.samples.get(key, SensorSample()).status in {"missing", "invalid"} for key in keys):
                return "missing"
            return "ready" if self.fresh(keys, now) else "stale"

    def snapshot(self, previous: Telemetry | None, utc: float, now: float) -> Telemetry:
        with self._lock:
            changes = {attribute: self.samples[key].value for key, attribute in ATTRIBUTES.items() if key in self.samples}
            ports = {key: sample.value for key, sample in self.samples.items() if key in PORT_KEYS and sample.value is not None}
            anderson = self.samples.get("anderson")
            if anderson is not None and anderson.value is not None:
                ports.update((f"anderson_{index + 1}", value) for index, value in enumerate(anderson.value))
            return replace(previous or Telemetry(None, None, None, utc, now), **changes, ports=tuple(ports.items()),
                           received_utc=utc, received_monotonic=now)

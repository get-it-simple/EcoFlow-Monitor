import threading
from uuid import uuid4

from ecoflow_monitor.adapters.ecoflow_mqtt import MqttCancellation
from ecoflow_monitor.device_profiles import CORE
from ecoflow_monitor.polling import PushedReading, TelemetryPatch


class MqttSession:
    def __init__(self, serial: str):
        self.serial = serial
        self.operation = uuid4().hex[:12]
        self.cancelled = MqttCancellation()
        self.deadline = float("inf")
        self.active = False
        self.readings: dict[str, PushedReading] = {}
        self.last_valid: dict[str, float] = {}
        self._lock = threading.Lock()
        self._pending: dict[str, PushedReading] = {}

    def offer(self, patch: TelemetryPatch, utc: float, now: float) -> bool:
        with self._lock:
            if self.cancelled.is_set():
                return False
            notify = not self._pending
            self._pending.update((key, PushedReading(value, utc, now)) for key, value in patch.values)
            self._pending.update((key, PushedReading(None, utc, now, False)) for key in patch.invalid)
            return notify and bool(self._pending)

    def take(self) -> dict[str, PushedReading]:
        with self._lock:
            pending, self._pending = self._pending, {}
        self.last_valid.update((key, value.monotonic) for key, value in pending.items() if value.valid)
        return pending

    def core_ready(self, now: float, stale_after: float) -> bool:
        return all(key in self.readings and self.readings[key].valid and
                   now - self.readings[key].monotonic < stale_after for key in CORE)

    def core_expired(self, now: float, stale_after: float) -> bool:
        return now >= self.core_deadline(stale_after)

    def core_deadline(self, stale_after: float) -> float:
        return min(self.last_valid[key] for key in CORE) + stale_after

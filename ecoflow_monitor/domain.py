from dataclasses import dataclass
from enum import Enum
import math
from pathlib import PureWindowsPath


class ConnectionState(str, Enum):
    UNCONFIGURED = "not_configured"
    CONNECTING = "connecting"
    LIVE = "connected"
    STALE = "stale_data"
    NETWORK_UNAVAILABLE = "internet_unavailable"
    DEVICE_OFFLINE = "device_offline"
    AUTH_ERROR = "authentication_error"
    UNSUPPORTED = "unsupported_device"
    API_ERROR = "api_unavailable"


class Availability(str, Enum):
    DISABLED = "msi_disabled"
    NOT_RUNNING = "msi_not_running"
    READY = "msi_ready"
    ACCESS_DENIED = "msi_access_denied"
    ERROR = "msi_error"


@dataclass(frozen=True)
class Telemetry:
    input_w: float
    output_w: float
    soc_pct: float
    received_utc: float
    received_monotonic: float
    min_soc: float | None = None
    max_soc: float | None = None
    ports: tuple[tuple[str, float], ...] = ()
    device_discharge_minutes: float | None = None
    device_charge_minutes: float | None = None
    charging_state: int | None = None


@dataclass(frozen=True)
class ResolvedLimit:
    value: float | None
    source: str


@dataclass(frozen=True)
class ProfileDefinition:
    slot: int
    label: str
    power_pct: float | None = None
    settings: tuple[tuple[str, str], ...] = ()
    source: str = "manual"


@dataclass(frozen=True)
class AfterburnerObservation:
    availability: Availability
    profiles: tuple[ProfileDefinition, ...] = ()
    gpu_ids: tuple[str, ...] = ()
    gpu_id: str = ""
    observed_slot: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class AutomationRule:
    id: str
    slot: int
    enabled: bool = True
    executable: str = ""
    input_condition: str = "any"
    min_soc: float | None = None
    max_soc: float | None = None

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id or type(self.slot) is not int or self.slot not in range(1, 6):
            raise ValueError("Invalid rule identity or profile slot")
        if type(self.enabled) is not bool or not isinstance(self.executable, str):
            raise ValueError("Invalid rule enabled state or executable")
        if self.input_condition not in {"any", "present", "absent"}:
            raise ValueError("Invalid input condition")
        for value in (self.min_soc, self.max_soc):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError("SOC bounds must be between 0 and 100")
        if self.min_soc is not None and self.max_soc is not None and self.min_soc >= self.max_soc:
            raise ValueError("SOC lower bound must be less than upper bound")


def normalized_executable(path: str) -> str:
    return str(PureWindowsPath(path)).casefold() if path else ""


def resolve_limit(api: float | None, manual: float | None) -> ResolvedLimit:
    if api is not None:
        return ResolvedLimit(api, "api")
    return ResolvedLimit(manual, "manual" if manual is not None else "unknown")


def runtime_hours(telemetry: Telemetry | None, capacity_wh: float, floor: float | None, fresh: bool) -> float | None:
    if not fresh or telemetry is None or floor is None:
        return None
    values = (capacity_wh, floor, telemetry.input_w, telemetry.output_w, telemetry.soc_pct)
    if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
        return None
    if capacity_wh <= 0 or not 0 <= floor <= 100 or not 0 <= telemetry.soc_pct <= 100:
        return None
    if telemetry.input_w != 0 or telemetry.output_w <= 0:
        return None
    return capacity_wh * max(telemetry.soc_pct - floor, 0) / 100 / telemetry.output_w


def select_rule(telemetry: Telemetry, running: frozenset[str], application_rules: tuple[AutomationRule, ...], system_rules: tuple[AutomationRule, ...]) -> AutomationRule | None:
    paths = {normalized_executable(path) for path in running}
    for rules, application in ((application_rules, True), (system_rules, False)):
        for rule in rules:
            if not rule.enabled or (application and normalized_executable(rule.executable) not in paths):
                continue
            if rule.input_condition == "present" and telemetry.input_w <= 0:
                continue
            if rule.input_condition == "absent" and telemetry.input_w != 0:
                continue
            if rule.min_soc is not None and telemetry.soc_pct < rule.min_soc:
                continue
            if rule.max_soc is not None and (telemetry.soc_pct > rule.max_soc or (telemetry.soc_pct == rule.max_soc and rule.max_soc != 100)):
                continue
            return rule
    return None


class AutomationGate:
    def __init__(self) -> None:
        self._candidate: int | None = None
        self._since = 0.0
        self._last_dispatch = -math.inf
        self._requested: int | None = None
        self._failed = False
        self._confirmed = False

    def reset(self) -> None:
        self._candidate = None
        self._requested = None
        self._failed = False
        self._confirmed = False

    def suspend(self) -> None:
        self._candidate = None
        self._requested = None
        self._failed = False
        self._confirmed = False

    def evaluate(self, slot: int | None, now: float, observed: int | None) -> int | None:
        if slot != self._candidate:
            self._candidate = slot
            self._since = now
            self._failed = False
        if slot is None or self._failed:
            return None
        if slot == self._requested:
            if observed == slot:
                self._confirmed = True
            if not self._confirmed or observed is None or observed == slot:
                return None
            self._requested = None
            self._confirmed = False
            self._since = now
        if slot == observed:
            return None
        if now - self._since < 5 or now - self._last_dispatch < 10:
            return None
        return slot

    def dispatched(self, slot: int, now: float, success: bool) -> None:
        self._last_dispatch = now
        self._requested = slot if success else None
        self._failed = not success
        self._confirmed = False

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import PureWindowsPath
from typing import Generic, Literal, Protocol, TypeVar


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


@dataclass(frozen=True)
class Telemetry:
    input_w: float | None
    output_w: float | None
    soc_pct: float | None
    received_utc: float
    received_monotonic: float
    min_soc: float | None = None
    max_soc: float | None = None
    ports: tuple[tuple[str, float], ...] = ()
    device_discharge_minutes: float | None = None
    device_charge_minutes: float | None = None
    charging_state: int | None = None
    ac_enabled: bool | None = None
    dc_enabled: bool | None = None


@dataclass(frozen=True)
class ResolvedLimit:
    value: float | None
    source: str


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


@dataclass(frozen=True)
class RemainingTimePresentation:
    mode: Literal["infinity", "waiting", "charge", "runtime"]
    hours: float | None = None
    charge_fraction: float | None = None


def present_remaining_time(hours: float | None, charging: bool, ac_power: float | None, ac_fresh: bool,
                           soc: float | None, soc_fresh: bool) -> RemainingTimePresentation:
    if ac_power is not None and not isinstance(ac_power, bool) and math.isfinite(ac_power) and ac_power > 0:
        if ac_fresh and soc_fresh and soc is not None and not isinstance(soc, bool) and math.isfinite(soc) and 0 <= soc <= 100:
            return RemainingTimePresentation("infinity", charge_fraction=soc / 100)
        return RemainingTimePresentation("waiting")
    if hours is not None:
        return RemainingTimePresentation("charge" if charging else "runtime", hours=hours)
    return RemainingTimePresentation("waiting")


def runtime_hours(telemetry: Telemetry | None, capacity_wh: float, floor: float | None, fresh: bool) -> float | None:
    if not fresh or telemetry is None or floor is None:
        return None
    values = (capacity_wh, floor, telemetry.input_w, telemetry.output_w, telemetry.soc_pct)
    if any(value is None or isinstance(value, bool) or not math.isfinite(value) for value in values):
        return None
    if capacity_wh <= 0 or not 0 <= floor <= 100 or not 0 <= telemetry.soc_pct <= 100:
        return None
    if telemetry.input_w != 0 or telemetry.output_w <= 0:
        return None
    return capacity_wh * max(telemetry.soc_pct - floor, 0) / 100 / telemetry.output_w


def charge_hours(telemetry: Telemetry | None, capacity_wh: float, ceiling: float | None, fresh: bool) -> float | None:
    if not fresh or telemetry is None or ceiling is None:
        return None
    values = (capacity_wh, ceiling, telemetry.input_w, telemetry.output_w, telemetry.soc_pct)
    if any(value is None or isinstance(value, bool) or not math.isfinite(value) for value in values):
        return None
    if capacity_wh <= 0 or not 0 <= ceiling <= 100 or not 0 <= telemetry.soc_pct <= 100:
        return None
    if telemetry.output_w < 0 or telemetry.input_w <= telemetry.output_w:
        return None
    return capacity_wh * max(ceiling - telemetry.soc_pct, 0) / 100 / (telemetry.input_w - telemetry.output_w)


class RuleConditions(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def executable(self) -> str: ...

    @property
    def input_condition(self) -> str: ...

    @property
    def min_soc(self) -> float | None: ...

    @property
    def max_soc(self) -> float | None: ...


RuleType = TypeVar("RuleType", bound=RuleConditions)
ProfileKey = TypeVar("ProfileKey", int, str)


def select_rule(telemetry: Telemetry, running: frozenset[str], application_rules: tuple[RuleType, ...], system_rules: tuple[RuleType, ...]) -> RuleType | None:
    paths = {normalized_executable(path) for path in running}
    for rules, application in ((application_rules, True), (system_rules, False)):
        for rule in rules:
            if not rule.enabled or (application and normalized_executable(rule.executable) not in paths):
                continue
            if rule.input_condition != "any" and telemetry.input_w is None:
                continue
            if (rule.min_soc is not None or rule.max_soc is not None) and telemetry.soc_pct is None:
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


class AutomationGate(Generic[ProfileKey]):
    def __init__(self) -> None:
        self._candidate: ProfileKey | None = None
        self._since = 0.0
        self._last_dispatch = -math.inf
        self._requested: ProfileKey | None = None
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

    def evaluate(self, slot: ProfileKey | None, now: float, observed: ProfileKey | None) -> ProfileKey | None:
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

    def dispatched(self, slot: ProfileKey, now: float, success: bool) -> None:
        self._last_dispatch = now
        self._requested = slot if success else None
        self._failed = not success
        self._confirmed = False

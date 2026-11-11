from dataclasses import dataclass
import math

from ecoflow_monitor.domain import ConnectionState, Telemetry


@dataclass(frozen=True)
class MotionState:
    mode: str = "unknown"
    status: str = "offline"
    input_active: bool = False
    output_active: bool = False
    balance: str | None = None
    ac_active: bool = False
    dc_active: bool = False
    soc: float | None = None
    input_share: float | None = None
    net: float | None = None


def finite(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def connection_status(connection: ConnectionState, fresh: bool) -> str:
    if fresh:
        return "online"
    if connection in {ConnectionState.LIVE, ConnectionState.STALE}:
        return "stale"
    if connection == ConnectionState.CONNECTING:
        return "connecting"
    if connection in {ConnectionState.UNCONFIGURED, ConnectionState.DEVICE_OFFLINE, ConnectionState.NETWORK_UNAVAILABLE}:
        return "offline"
    return "error"


def derive_motion(telemetry: Telemetry | None, connection: ConnectionState, fresh: bool, previous: str, thresholds: dict) -> MotionState:
    status = connection_status(connection, fresh)
    if telemetry is None:
        return MotionState(status=status)
    incoming = telemetry.input_w if finite(telemetry.input_w) and telemetry.input_w >= 0 else None
    outgoing = telemetry.output_w if finite(telemetry.output_w) and telemetry.output_w >= 0 else None
    soc = max(0, min(100, telemetry.soc_pct)) if finite(telemetry.soc_pct) else None
    net = incoming - outgoing if incoming is not None and outgoing is not None else None
    share = incoming / (incoming + outgoing) if net is not None and incoming + outgoing > 0 else 0.5 if net is not None else None
    mode = "unknown"
    balance = None
    if fresh and net is not None:
        if net > thresholds["enterDirectionAbove"] or previous == "charging" and net > thresholds["retainDirectionAbove"]:
            mode, balance = "charging", "input"
        elif net < -thresholds["enterDirectionAbove"] or previous == "discharging" and net < -thresholds["retainDirectionAbove"]:
            mode, balance = "discharging", "output"
        else:
            mode = "idle"
        if soc is None:
            mode = "unknown"
    if fresh and soc is not None and type(telemetry.charging_state) is int:
        mode = {0: "idle", 1: "discharging", 2: "charging"}.get(telemetry.charging_state, mode)
    return MotionState(
        mode=mode, status=status, balance=balance, soc=soc, input_share=share, net=net,
        input_active=fresh and incoming is not None and incoming > thresholds["activeAbove"],
        output_active=fresh and outgoing is not None and outgoing > thresholds["activeAbove"],
        ac_active=fresh and telemetry.ac_enabled is True,
        dc_active=fresh and telemetry.dc_enabled is True,
    )


class ValueTransition:
    def __init__(self):
        self.origin = None
        self.target = None
        self.started = 0.0
        self.duration = 0.0

    def value(self, now):
        if self.origin is None or self.target is None or not self.duration:
            return self.target
        progress = min(1, max(0, (now - self.started) / self.duration))
        eased = 1 - (1 - progress) ** 3
        return self.origin + (self.target - self.origin) * eased

    def set(self, target, now, duration):
        if target == self.target and duration:
            return
        self.origin = self.value(now)
        self.target = target
        self.started = now
        self.duration = duration if self.origin is not None and target is not None else 0

    def active(self, now):
        return self.duration > 0 and self.origin != self.target and now < self.started + self.duration


class RoleTransition:
    def __init__(self):
        self.old = None
        self.target = None
        self.started = 0.0
        self.exit_ms = 0
        self.enter_ms = 0
        self.old_gain = 1.0

    def sample(self, now):
        elapsed = max(0, now - self.started)
        if self.old is not None and elapsed < self.exit_ms:
            return self.old, self.old_gain * (1 - elapsed / self.exit_ms)
        gain = min(1, (elapsed - self.exit_ms) / self.enter_ms) if self.enter_ms else 1
        return self.target, max(0, gain) if self.target is not None else 0

    def set(self, role, now, timing, enabled):
        if role == self.target and enabled:
            return
        self.old, self.old_gain = self.sample(now)
        self.target = role
        self.started = now
        self.exit_ms = timing["directionExit"] if enabled and self.old is not None else 0
        self.enter_ms = (timing["directionEnter"] if self.old is not None else timing["visibilityTransition"]) if enabled else 0
        if not enabled:
            self.old = None


from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from ecoflow_monitor.integrations import IntegrationDefinition, afterburner_example, hotkey_actions, migrate_afterburner


METRICS = ("battery_level", "input_power", "output_power", "estimated_runtime", "minimum_discharge_level", "maximum_charge_level")
HOTKEY_ACTIONS = ("toggle_monitor", "toggle_mini")


@dataclass(frozen=True)
class HotkeyBinding:
    action: str
    combo: str = ""
    enabled: bool = False


def default_hotkeys() -> tuple[HotkeyBinding, ...]:
    return tuple(HotkeyBinding(action, "ctrl+alt+num5" if action == "toggle_monitor" else "", action == "toggle_monitor") for action in HOTKEY_ACTIONS)


@dataclass(frozen=True)
class AppSettings:
    serial: str = ""
    credential_ref: str = field(default_factory=lambda: "EcoFlowMonitor/" + uuid.uuid4().hex)
    capacity_wh: float = 2048
    manual_min: float | None = None
    manual_max: float | None = None
    integrations: tuple[IntegrationDefinition, ...] = field(default_factory=lambda: (afterburner_example(),))
    hotkeys: tuple[HotkeyBinding, ...] = field(default_factory=default_hotkeys)
    language: str = "system"
    scale: float = 1
    transparency: bool = True
    background_opacity: float = 140 / 255
    animations: bool = True
    monitor_visible: bool = True
    monitor_geometry: tuple[int, int, int, int] = (-1, -1, 660, 820)
    mini_enabled: bool = False
    mini_metrics: tuple[str, ...] = ("battery_level", "input_power", "output_power")
    mini_scale: float = 1
    mini_transparency: float = 0.8
    mini_position: tuple[int, int] = (-1, -1)
    mini_dock: str = ""
    mini_above_taskbar: bool = False

    def validate(self) -> None:
        if any(type(value) is not bool for value in (self.transparency, self.animations, self.monitor_visible, self.mini_enabled, self.mini_above_taskbar)):
            raise ValueError("Boolean preference expected")
        if any(not isinstance(value, str) for value in (self.serial, self.credential_ref, self.language)):
            raise ValueError("Text preference expected")
        for value, minimum, maximum in ((self.capacity_wh, 1, 1000000), (self.scale, 0.5, 3), (self.mini_scale, 0.5, 3), (self.mini_transparency, 0, 1), (self.background_opacity, 0, 1)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("Invalid numeric setting")
        for value in (self.manual_min, self.manual_max):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError("Invalid manual limit")
        if self.manual_min is not None and self.manual_max is not None and self.manual_min >= self.manual_max:
            raise ValueError("Discharge limit must be below charge limit")
        if not self.credential_ref.startswith("EcoFlowMonitor/") or not self.language:
            raise ValueError("Invalid credential reference or language")
        if self.mini_dock not in {"", "left", "right", "top", "bottom"}:
            raise ValueError("Invalid docking edge")
        if not self.mini_metrics or len(set(self.mini_metrics)) != len(self.mini_metrics) or any(value not in METRICS for value in self.mini_metrics):
            raise ValueError("Invalid mini widget metrics")
        ids = set()
        for module in self.integrations:
            module.validate()
            if module.id in ids:
                raise ValueError("Duplicate integration ID")
            ids.add(module.id)
        actions = hotkey_actions(self.integrations)
        if len({binding.action for binding in self.hotkeys}) != len(self.hotkeys) or any(binding.action not in actions for binding in self.hotkeys):
            raise ValueError("Invalid hotkey actions")
        if any(not isinstance(binding.combo, str) or type(binding.enabled) is not bool for binding in self.hotkeys):
            raise ValueError("Invalid shortcut preference")
        for geometry in (self.monitor_geometry, self.mini_position):
            if any(isinstance(value, bool) or not isinstance(value, int) or abs(value) > 100000 for value in geometry):
                raise ValueError("Invalid window geometry")
        if len(self.monitor_geometry) != 4 or len(self.mini_position) != 2 or min(self.monitor_geometry[2:]) <= 0:
            raise ValueError("Invalid window dimensions")

    def to_dict(self) -> dict:
        self.validate()
        return {
            "version": 2,
            "ecoflow": {"serial": self.serial, "credential_ref": self.credential_ref, "capacity_wh": self.capacity_wh, "manual_min": self.manual_min, "manual_max": self.manual_max},
            "integrations": [module.to_dict() for module in self.integrations],
            "hotkeys": [asdict(binding) for binding in self.hotkeys],
            "language": self.language,
            "appearance": {"scale": self.scale, "transparency": self.transparency, "background_opacity": self.background_opacity, "animations": self.animations, "visible": self.monitor_visible, "geometry": list(self.monitor_geometry)},
            "mini_widget": {"enabled": self.mini_enabled, "metrics": list(self.mini_metrics), "scale": self.mini_scale, "transparency": self.mini_transparency, "position": list(self.mini_position), "dock": self.mini_dock, "above_taskbar": self.mini_above_taskbar},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        if not isinstance(data, dict) or type(data.get("version", 0)) is not int or data.get("version", 0) not in (0, 1, 2):
            raise ValueError("Unsupported settings schema")
        defaults = cls()
        eco = data.get("ecoflow", {})
        msi = data.get("afterburner", {})
        automation = data.get("automation", {})
        appearance = data.get("appearance", {})
        mini = data.get("mini_widget", {})
        if any(not isinstance(group, dict) for group in (eco, msi, automation, appearance, mini)):
            raise ValueError("Settings groups must be objects")
        def boolean(group, key, fallback):
            value = group.get(key, fallback)
            if type(value) is not bool:
                raise ValueError("Boolean setting expected")
            return value
        def string(group, key, fallback=""):
            value = group.get(key, fallback)
            if not isinstance(value, str):
                raise ValueError("Text setting expected")
            return value
        modules = tuple(IntegrationDefinition.from_dict(item) for item in data["integrations"]) if data.get("version") == 2 else (migrate_afterburner(msi, automation),)
        bindings = data.get("hotkeys", [asdict(binding) for binding in defaults.hotkeys])
        if data.get("version") != 2:
            bindings = [dict(binding, action=("auto:afterburner" if binding["action"] == "toggle_auto" else "profile:afterburner:" + binding["action"] if binding["action"].startswith("profile_") else binding["action"])) for binding in bindings]
        settings = cls(
            serial=string(eco, "serial"), credential_ref=string(eco, "credential_ref", defaults.credential_ref),
            capacity_wh=eco.get("capacity_wh", 2048), manual_min=eco.get("manual_min"), manual_max=eco.get("manual_max"),
            integrations=modules, hotkeys=tuple(HotkeyBinding(**binding) for binding in bindings),
            language=string(data, "language", "system"), scale=appearance.get("scale", data.get("ui_scale", 1)),
            transparency=boolean(appearance, "transparency", True), animations=boolean(appearance, "animations", True),
            background_opacity=appearance.get("background_opacity", defaults.background_opacity),
            monitor_visible=boolean(appearance, "visible", True), monitor_geometry=tuple(appearance.get("geometry", defaults.monitor_geometry)),
            mini_enabled=boolean(mini, "enabled", False), mini_metrics=tuple(mini.get("metrics", defaults.mini_metrics)),
            mini_scale=mini.get("scale", 1), mini_transparency=mini.get("transparency", 0.8),
            mini_position=tuple(mini.get("position", defaults.mini_position)), mini_dock=string(mini, "dock"),
            mini_above_taskbar=boolean(mini, "above_taskbar", False),
        )
        settings.validate()
        return settings


class SettingsError(Exception):
    pass


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.recovery = False

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            return AppSettings.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.recovery = True
            raise SettingsError("settings_load_error") from None

    def save(self, settings: AppSettings, explicit: bool = False) -> None:
        if self.recovery and not explicit:
            raise SettingsError("settings_recovery")
        temporary: str | None = None
        try:
            content = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.recovery and self.path.exists():
                shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".ecoflow-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.recovery = False
        except (OSError, ValueError, TypeError):
            raise SettingsError("settings_save_error") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def default_settings_path() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    if getattr(sys, "frozen", False) and (base / ".ecoflow-monitor-installed").is_file():
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise SettingsError("settings_save_error")
        return Path(local) / "EcoFlowMonitor" / "settings.json"
    try:
        with tempfile.TemporaryFile(dir=base):
            pass
        return base / "settings.json"
    except OSError:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise SettingsError("settings_save_error") from None
        return Path(local) / "EcoFlowMonitor" / "settings.json"

from dataclasses import asdict, dataclass, field, replace
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from ecoflow_monitor.integrations import IntegrationDefinition, afterburner_example, hotkey_actions, migrate_afterburner
from ecoflow_monitor.credential_refs import normalize_secret_reference


METRICS = ("battery_level", "input_power", "output_power", "estimated_runtime", "minimum_discharge_level", "maximum_charge_level")
HOTKEY_ACTIONS = ("toggle_monitor", "toggle_mini", "open_dashboard", "recover_widgets", "toggle_main")


@dataclass(frozen=True)
class HotkeyBinding:
    action: str
    combo: str = ""
    enabled: bool = False


def default_hotkeys() -> tuple[HotkeyBinding, ...]:
    combos = {"toggle_monitor": "ctrl+alt+num5", "recover_widgets": "ctrl+alt+w"}
    return tuple(HotkeyBinding(action, combos.get(action, ""), action in combos) for action in HOTKEY_ACTIONS)


@dataclass(frozen=True)
class OverlayPreferences:
    pinned: bool = True
    position_locked: bool = False
    click_through: bool = False
    screen: str = ""
    dpi: float = 96

    def validate(self) -> None:
        if any(type(value) is not bool for value in (self.pinned, self.position_locked, self.click_through)):
            raise ValueError("Boolean overlay preference expected")
        if not isinstance(self.screen, str) or isinstance(self.dpi, bool) or not isinstance(self.dpi, (int, float)) or not math.isfinite(self.dpi) or not 24 <= self.dpi <= 960:
            raise ValueError("Invalid display context")

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid overlay preferences")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True)
class WidgetPreferences:
    enabled: bool = False
    scale: float = 1
    opacity: float = 0.82
    geometry: tuple[int, int, int, int] = (-1, -1, 512, 350)
    overlay: OverlayPreferences = field(default_factory=OverlayPreferences)
    dock: str = ""
    above_taskbar: bool = False
    show_device_name: bool = False
    show_connection_status: bool = False
    labels_visible: bool = True
    values_on_hover: bool = False
    small_metrics: tuple[str, ...] = METRICS[:3]
    large_metrics: tuple[str, ...] = METRICS
    expanded_metrics: tuple[str, ...] = ()
    metric_order: tuple[str, ...] = METRICS

    def validate(self) -> None:
        if not isinstance(self.overlay, OverlayPreferences):
            raise ValueError("Invalid widget overlay preferences")
        self.overlay.validate()
        if any(type(value) is not bool for value in (self.enabled, self.above_taskbar, self.show_device_name, self.show_connection_status, self.labels_visible, self.values_on_hover)):
            raise ValueError("Boolean widget preference expected")
        for value, lower, upper in ((self.scale, 0.5, 3), (self.opacity, 0, 1)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError("Invalid widget appearance")
        if self.dock not in {"", "left", "right", "top", "bottom"}:
            raise ValueError("Invalid widget docking")
        if not isinstance(self.geometry, tuple) or len(self.geometry) != 4 or any(type(value) is not int for value in self.geometry) or min(self.geometry[2:]) <= 0:
            raise ValueError("Invalid widget geometry")
        for group in (self.small_metrics, self.large_metrics, self.expanded_metrics, self.metric_order):
            if not isinstance(group, tuple) or any(not isinstance(key, str) for key in group) or len(set(group)) != len(group) or any(key not in METRICS for key in group):
                raise ValueError("Invalid widget metrics")
        if set(self.metric_order) != set(METRICS) or not set(self.small_metrics) <= set(self.large_metrics) or set(self.large_metrics) & set(self.expanded_metrics) or not (self.large_metrics or self.expanded_metrics):
            raise ValueError("Invalid widget visibility hierarchy")

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Widget preferences must be an object")
        values = dict(data)
        values["overlay"] = OverlayPreferences.from_dict(values.get("overlay", {}))
        for key in ("geometry", "small_metrics", "large_metrics", "expanded_metrics", "metric_order"):
            if key in values:
                if not isinstance(values[key], (list, tuple)):
                    raise ValueError("Widget sequences must be arrays")
                values[key] = tuple(values[key])
        try:
            result = cls(**values)
        except TypeError as error:
            raise ValueError("Invalid widget preferences") from error
        result.validate()
        return result


@dataclass(frozen=True)
class AppSettings:
    serial: str = ""
    credential_ref: str = field(default_factory=lambda: "EcoFlowMonitor/" + uuid.uuid4().hex)
    credential_source: str = "windows"
    onepassword_access_ref: str = ""
    onepassword_secret_ref: str = ""
    onepassword_cache_ttl_seconds: int = 86400
    capacity_wh: float = 2048
    manual_min: float | None = None
    manual_max: float | None = None
    integrations: tuple[IntegrationDefinition, ...] = field(default_factory=lambda: (afterburner_example(),))
    hotkeys: tuple[HotkeyBinding, ...] = field(default_factory=default_hotkeys)
    language: str = "system"
    logging_mode: str = "normal"
    scale: float = 1
    transparency: bool = True
    background_opacity: float = 0.82
    animations: bool = True
    monitor_visible: bool = True
    monitor_labels_visible: bool = True
    monitor_values_on_hover: bool = False
    monitor_geometry: tuple[int, int, int, int] = (-1, -1, 512, 350)
    mini_enabled: bool = False
    mini_metrics: tuple[str, ...] = ("battery_level", "input_power", "output_power")
    mini_hidden_metrics: tuple[str, ...] = ()
    mini_scale: float = 1
    mini_transparency: float = 0.18
    mini_position: tuple[int, int] = (-1, -1)
    mini_dock: str = ""
    mini_above_taskbar: bool = False
    main_overlay: OverlayPreferences = field(default_factory=OverlayPreferences)
    mini_overlay: OverlayPreferences = field(default_factory=OverlayPreferences)
    dashboard_geometry: tuple[int, int, int, int] = (-1, -1, 1184, 780)
    metric_order: tuple[str, ...] = METRICS
    manual_min_retained: float = 20
    manual_max_retained: float = 90
    main_enabled: bool = False
    main_scale: float = 1
    widget: WidgetPreferences = field(default_factory=WidgetPreferences)

    def validate(self) -> None:
        self.widget.validate()
        self.main_overlay.validate()
        self.mini_overlay.validate()
        if not isinstance(self.logging_mode, str) or self.logging_mode not in {"off", "normal", "detailed"}:
            raise ValueError("Invalid logging mode")
        if len(self.metric_order) != len(METRICS) or set(self.metric_order) != set(METRICS):
            raise ValueError("Invalid metric order")
        if any(type(value) is not bool for value in (self.transparency, self.animations, self.main_enabled, self.monitor_visible, self.monitor_labels_visible, self.monitor_values_on_hover, self.mini_enabled, self.mini_above_taskbar)):
            raise ValueError("Boolean preference expected")
        if any(not isinstance(value, str) for value in (self.serial, self.credential_ref, self.language, self.credential_source, self.onepassword_access_ref, self.onepassword_secret_ref)):
            raise ValueError("Text preference expected")
        if self.credential_source not in {"windows", "1password"}:
            raise ValueError("Invalid credential source")
        if type(self.onepassword_cache_ttl_seconds) is not int or not 3600 <= self.onepassword_cache_ttl_seconds <= 604800 or self.onepassword_cache_ttl_seconds % 3600:
            raise ValueError("onepassword_cache_ttl_error")
        for reference in (self.onepassword_access_ref, self.onepassword_secret_ref):
            if reference or self.credential_source == "1password":
                if normalize_secret_reference(reference) != reference:
                    raise ValueError("onepassword_reference_error")
        for value, minimum, maximum in ((self.capacity_wh, 1, 1000000), (self.scale, 0.5, 3), (self.main_scale, 0.5, 3), (self.mini_scale, 0.5, 3), (self.mini_transparency, 0, 1), (self.background_opacity, 0, 1)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("Invalid numeric setting")
        for value in (self.manual_min, self.manual_max, self.manual_min_retained, self.manual_max_retained):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError("Invalid manual limit")
        if self.manual_min is not None and self.manual_max is not None and self.manual_min >= self.manual_max:
            raise ValueError("Discharge limit must be below charge limit")
        if not self.credential_ref.startswith("EcoFlowMonitor/") or not self.language:
            raise ValueError("Invalid credential reference or language")
        if self.mini_dock not in {"", "left", "right", "top", "bottom"}:
            raise ValueError("Invalid docking edge")
        mini_metrics = self.mini_metrics + self.mini_hidden_metrics
        if not mini_metrics or len(set(mini_metrics)) != len(mini_metrics) or any(value not in METRICS for value in mini_metrics):
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
        for geometry in (self.monitor_geometry, self.mini_position, self.dashboard_geometry):
            if any(isinstance(value, bool) or not isinstance(value, int) or abs(value) > 100000 for value in geometry):
                raise ValueError("Invalid window geometry")
        if len(self.monitor_geometry) != 4 or len(self.mini_position) != 2 or min(self.monitor_geometry[2:]) <= 0:
            raise ValueError("Invalid window dimensions")
        if len(self.dashboard_geometry) != 4 or min(self.dashboard_geometry[2:]) <= 0:
            raise ValueError("Invalid dashboard dimensions")

    def to_dict(self) -> dict:
        self.validate()
        return {
            "version": 4,
            "widget": asdict(self.widget),
            "workspace": {"main_enabled": self.main_enabled, "main_scale": self.main_scale, "dashboard_geometry": list(self.dashboard_geometry), "main_overlay": asdict(self.main_overlay), "mini_overlay": asdict(self.mini_overlay), "metric_order": list(self.metric_order), "manual_min_retained": self.manual_min_retained, "manual_max_retained": self.manual_max_retained},
            "ecoflow": {"serial": self.serial, "credential_ref": self.credential_ref, "credential_source": self.credential_source,
                        "onepassword": {"access_ref": self.onepassword_access_ref, "secret_ref": self.onepassword_secret_ref, "cache_ttl_seconds": self.onepassword_cache_ttl_seconds},
                        "capacity_wh": self.capacity_wh, "manual_min": self.manual_min, "manual_max": self.manual_max},
            "integrations": [module.to_dict() for module in self.integrations],
            "hotkeys": [asdict(binding) for binding in self.hotkeys],
            "language": self.language,
            "logging": {"mode": self.logging_mode},
            "appearance": {"scale": self.scale, "transparency": self.transparency, "background_opacity": self.background_opacity, "animations": self.animations, "visible": self.monitor_visible, "geometry": list(self.monitor_geometry), "monitor_labels_visible": self.monitor_labels_visible, "monitor_values_on_hover": self.monitor_values_on_hover},
            "mini_widget": {"enabled": self.mini_enabled, "metrics": list(self.mini_metrics), "hidden_metrics": list(self.mini_hidden_metrics), "scale": self.mini_scale, "transparency": self.mini_transparency, "position": list(self.mini_position), "dock": self.mini_dock, "above_taskbar": self.mini_above_taskbar},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        if not isinstance(data, dict) or type(data.get("version", 0)) is not int or data.get("version", 0) not in (0, 1, 2, 3, 4):
            raise ValueError("Unsupported settings schema")
        defaults = cls()
        eco = data.get("ecoflow", {})
        msi = data.get("afterburner", {})
        automation = data.get("automation", {})
        appearance = data.get("appearance", {})
        mini = data.get("mini_widget", {})
        workspace = data.get("workspace", {})
        diagnostics = data.get("logging", {})
        if any(not isinstance(group, dict) for group in (eco, msi, automation, appearance, mini, workspace, diagnostics)):
            raise ValueError("Settings groups must be objects")
        onepassword = eco.get("onepassword", {})
        if not isinstance(onepassword, dict):
            raise ValueError("1Password settings must be an object")
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
        modules = tuple(IntegrationDefinition.from_dict(item) for item in data["integrations"]) if data.get("version", 0) >= 2 else (migrate_afterburner(msi, automation),)
        bindings = data.get("hotkeys", [asdict(binding) for binding in defaults.hotkeys])
        if data.get("version", 0) < 2:
            bindings = [dict(binding, action=("auto:afterburner" if binding["action"] == "toggle_auto" else "profile:afterburner:" + binding["action"] if binding["action"].startswith("profile_") else binding["action"])) for binding in bindings]
        if data.get("version", 0) < 3:
            existing_actions = {binding["action"] for binding in bindings}
            bindings = [*bindings, *(asdict(binding) for binding in default_hotkeys() if binding.action in {"open_dashboard", "recover_widgets"} and binding.action not in existing_actions)]
        settings = cls(
            widget=WidgetPreferences.from_dict(data.get("widget", {})),
            serial=string(eco, "serial"), credential_ref=string(eco, "credential_ref", defaults.credential_ref),
            credential_source=string(eco, "credential_source", "windows"),
            onepassword_access_ref=string(onepassword, "access_ref"), onepassword_secret_ref=string(onepassword, "secret_ref"),
            onepassword_cache_ttl_seconds=onepassword.get("cache_ttl_seconds", 86400),
            capacity_wh=eco.get("capacity_wh", 2048), manual_min=eco.get("manual_min"), manual_max=eco.get("manual_max"),
            integrations=modules, hotkeys=tuple(HotkeyBinding(**binding) for binding in bindings),
            language=string(data, "language", "system"), scale=appearance.get("scale", data.get("ui_scale", 1)),
            logging_mode=string(diagnostics, "mode", "normal"),
            transparency=boolean(appearance, "transparency", True), animations=boolean(appearance, "animations", True),
            main_enabled=boolean(workspace, "main_enabled", False),
            main_scale=workspace.get("main_scale", appearance.get("scale", data.get("ui_scale", 1))),
            background_opacity=appearance.get("background_opacity", 140 / 255 if data.get("version", 0) < 3 else defaults.background_opacity),
            monitor_visible=boolean(appearance, "visible", True), monitor_geometry=tuple(appearance.get("geometry", defaults.monitor_geometry)),
            monitor_labels_visible=boolean(appearance, "monitor_labels_visible", True),
            monitor_values_on_hover=boolean(appearance, "monitor_values_on_hover", False),
            mini_enabled=boolean(mini, "enabled", False), mini_metrics=tuple(mini.get("metrics", defaults.mini_metrics)),
            mini_hidden_metrics=tuple(mini.get("hidden_metrics", ())),
            mini_scale=mini.get("scale", 1), mini_transparency=mini.get("transparency", 0.8 if data.get("version", 0) < 3 else defaults.mini_transparency),
            mini_position=tuple(mini.get("position", defaults.mini_position)), mini_dock=string(mini, "dock"),
            mini_above_taskbar=boolean(mini, "above_taskbar", False),
            main_overlay=OverlayPreferences.from_dict(workspace.get("main_overlay", {})),
            mini_overlay=OverlayPreferences.from_dict(workspace.get("mini_overlay", {})),
            dashboard_geometry=tuple(workspace.get("dashboard_geometry", defaults.dashboard_geometry)),
            metric_order=tuple(workspace.get("metric_order", tuple(dict.fromkeys(tuple(mini.get("metrics", defaults.mini_metrics)) + tuple(mini.get("hidden_metrics", ())) + METRICS)))),
            manual_min_retained=workspace.get("manual_min_retained", eco.get("manual_min") if eco.get("manual_min") is not None else 20),
            manual_max_retained=workspace.get("manual_max_retained", eco.get("manual_max") if eco.get("manual_max") is not None else 90),
        )
        if data.get("version", 0) < 4:
            settings = replace(settings, widget=WidgetPreferences(
                enabled=settings.main_enabled or settings.mini_enabled,
                scale=settings.main_scale,
                opacity=settings.background_opacity if settings.transparency else 1,
                geometry=settings.monitor_geometry,
                overlay=settings.main_overlay,
                labels_visible=settings.monitor_labels_visible,
                values_on_hover=settings.monitor_values_on_hover,
                small_metrics=settings.mini_metrics,
                large_metrics=settings.mini_metrics,
                expanded_metrics=settings.mini_hidden_metrics,
                metric_order=settings.metric_order,
            ))
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

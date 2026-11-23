from dataclasses import dataclass, field, replace
import hmac
import logging
import math
import secrets
import threading
import time
import uuid
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal

from ecoflow_monitor import __version__, i18n
from ecoflow_monitor.diagnostics import DiagnosticLog, event
from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation, TransitionResult, integration_process_names, running_processes
from ecoflow_monitor.adapters.process_events import ProcessTracker, relevant_processes
from ecoflow_monitor.adapters.credentials import CredentialError, CredentialStore
from ecoflow_monitor.adapters.ecoflow import Credentials, Device, EcoFlowClient, EcoFlowError
from ecoflow_monitor.adapters.ecoflow_mqtt import BrokerAccess, MqttError, stream_telemetry, valid_topic_segment
from ecoflow_monitor.adapters.onepassword import read_credentials
from ecoflow_monitor.adapters.hotkeys import validate_bindings
from ecoflow_monitor.adapters.windows import internet_unavailable
from ecoflow_monitor.credential_cache import SessionCredentialCache
from ecoflow_monitor.device_metadata import DeviceMetadataStore
from ecoflow_monitor.device_profiles import BASELINE, ProfileCatalog, builtin_catalog, load_catalog
from ecoflow_monitor.domain import AutomationGate, ConnectionState, charge_hours, resolve_limit, runtime_hours
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile, reaction_value_keys
from ecoflow_monitor.integration_state import IntegrationStateStore, ProcessIdentity, command_fingerprint
from ecoflow_monitor.mqtt_session import MqttAccess, MqttSession
from ecoflow_monitor.settings import AppSettings, SettingsError, SettingsStore
from ecoflow_monitor.polling import CORE, DASHBOARD_SENSORS, SENSOR_FIELDS, PollingState, TelemetryPatch, dependencies
from ecoflow_monitor.system_activity import ActivityCancelled, SystemActivity
from ecoflow_monitor.telemetry_export import TelemetryExport
from ecoflow_monitor.telemetry_values import app_readings, value_dependencies


logger = logging.getLogger(__name__)
MQTT_RETRY_INITIAL = 5.0
MQTT_RETRY_MAX = 60.0


class TaskSignals(QObject):
    finished = Signal(str, int, object, object)


class Task(QRunnable):
    def __init__(self, kind: str, generation: int, operation: Callable, cancelled=None) -> None:
        super().__init__()
        self.kind = kind
        self.generation = generation
        self.operation = operation
        self.cancelled = cancelled
        self.signals = TaskSignals()

    def run(self) -> None:
        result = None
        failure = None
        try:
            if self.cancelled is not None and self.cancelled.is_set():
                raise ActivityCancelled()
            result = self.operation()
        except Exception as error:
            failure = error
        self.signals.finished.emit(self.kind, self.generation, result, failure)


@dataclass
class IntegrationState:
    observation: IntegrationObservation = field(default_factory=lambda: IntegrationObservation("integration_disabled"))
    active_profile: str | None = None
    active_process: ProcessIdentity | None = None
    requested_profile: str | None = None
    transition_from: str | None = None
    result: TransitionResult | None = None
    busy: bool = False
    dispatch_revision: int = 0
    gate: AutomationGate[str] = field(default_factory=AutomationGate)
    next_scan: float = 0
    auto_reason: str = "automation_paused"


@dataclass(frozen=True)
class DiscoverySnapshot:
    devices: tuple
    settings: AppSettings = field(repr=False)
    credential_identity: bytes = field(repr=False)

    def matches(self, settings, credential_identity):
        if settings.credential_source != self.settings.credential_source:
            return False
        if settings.credential_source == "1password":
            return (settings.onepassword_access_ref, settings.onepassword_secret_ref) == (self.settings.onepassword_access_ref, self.settings.onepassword_secret_ref)
        return hmac.compare_digest(credential_identity, self.credential_identity) if credential_identity is not None else settings.credential_ref == self.settings.credential_ref


@dataclass(frozen=True)
class MqttSetup:
    catalog: tuple[Device, ...] | None = None
    profiles: ProfileCatalog | None = None
    access: MqttAccess | None = None
    failure: EcoFlowError | MqttError | None = None
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @property
    def denied(self) -> bool:
        return isinstance(self.failure, EcoFlowError) and self.failure.state == ConnectionState.AUTH_ERROR


class Controller(QObject):
    changed = Signal()
    preferences_changed = Signal()
    runtime_preferences_changed = Signal(object)
    devices_ready = Signal(object)
    elevation_needed = Signal(str)
    credentials_expired = Signal()
    quotas_started = Signal()
    task_finished = Signal(str)
    unsupported_device = Signal()
    system_activity_changed = Signal(object)
    profiles_ready = Signal(object)
    profile_example_ready = Signal(object, object)
    mqtt_readings_ready = Signal(object)
    credential_cache_updated = Signal()

    def __init__(self, store: SettingsStore, credential_store=None, integration_adapter=None, client_factory=None, clock=time.monotonic, credential_cache=None, diagnostics: DiagnosticLog | None = None, *, mqtt_stream=stream_telemetry, native_process_events=False) -> None:
        super().__init__()
        self.store = store
        self.credential_store = credential_store if credential_store is not None else CredentialStore()
        self.integration_adapter = integration_adapter if integration_adapter is not None else IntegrationAdapter()
        self._shared_process_snapshot = integration_adapter is None
        self.client_factory = client_factory if client_factory is not None else EcoFlowClient
        self.clock = clock
        self.credential_cache = credential_cache if credential_cache is not None else SessionCredentialCache(clock=clock)
        self.credential_renewal_required = False
        self.credential_renewal_pending = False
        self.interactions = None
        self.access_allowed = True
        self.catalog = None
        self._discovery_snapshot = None
        self._discovery_key = secrets.token_bytes(32)
        self.metadata = DeviceMetadataStore(store.path.with_name("device-metadata.json"))
        self._unsupported_notified = set()
        self._diagnostic_failures = ()
        self._metadata_dirty = False
        self.notice = ""
        self.startup_pending = False
        self.preview = None
        self.system_activity = self._reported_activity = SystemActivity()
        self._activity_epoch = 0
        self._resume_fields = None
        self._resume_scans = set()
        self._started = False
        try:
            self.settings = store.load()
        except SettingsError as error:
            self.settings = AppSettings()
            self.notice = str(error)
        self.diagnostics = diagnostics
        self.telemetry_export = TelemetryExport(self.settings.telemetry_export, clock)
        self._export_deadline = math.inf
        self._last_export = None
        self.profile_catalog = None if self.settings.custom_device_profiles else builtin_catalog()
        if self.diagnostics is not None:
            self.diagnostics.configure(self.settings.logging_mode)
        if self.notice:
            event("settings_load_failed", level=logging.WARNING)
        i18n.setup(self.settings.language)
        self.connection = ConnectionState.UNCONFIGURED
        self.telemetry = None
        self.polling = PollingState(self.settings.polling)
        self._demands = {"dashboard": DASHBOARD_SENSORS}
        self._polling_revision = 0
        self.quotas_started.connect(self.changed, Qt.ConnectionType.QueuedConnection)
        self._update_demand()
        self.integration_states = {module.id: IntegrationState() for module in self.settings.integrations}
        self.integration_cache = IntegrationStateStore(store.path.with_name("integration-state.json"))
        self.integration_cache.configure(self.settings.integrations)
        self._generation = 0
        self._stopped = False
        self._credential_cancelled = threading.Event()
        self._work_cancelled = threading.Event()
        self._active = {}
        self._api_pending = False
        self._discovery_pending = None
        self._diagnostic_pending = False
        self._api_mode = "telemetry"
        self._next_api = 0.0
        self._retry = 5.0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(3)
        self._mqtt_stream = mqtt_stream
        self._mqtt_pool = QThreadPool(self)
        self._mqtt_pool.setMaxThreadCount(1)
        self._mqtt_session: MqttSession | None = None
        self._mqtt_access: MqttAccess | None = None
        self._mqtt_auth_blocked = False
        self._mqtt_certification_retry_at = 0.0
        self.mqtt_access_status = ""
        self._mqtt_retry_at = 0.0
        self._mqtt_retry_delay = MQTT_RETRY_INITIAL
        self._mqtt_fallback = False
        self._telemetry_epoch = 0
        self.mqtt_readings_ready.connect(self._accept_mqtt, Qt.ConnectionType.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.tick)
        self.preferences_changed.connect(self.wake)
        self.quotas_started.connect(self.wake, Qt.ConnectionType.QueuedConnection)
        self.credential_cache_updated.connect(self.wake, Qt.ConnectionType.QueuedConnection)
        self._processes = ProcessTracker(lambda: self.clock(), self) if native_process_events and integration_adapter is None else None
        self._inspected_process_revision = -1
        if self._processes is not None:
            self._processes.changed.connect(self.wake)
        self._activity_timer = QTimer(self)
        self._activity_timer.setSingleShot(True)
        self._activity_timer.timeout.connect(self._apply_system_activity)
        self.changed.connect(self._update_export)

    def set_system_activity(self, activity: SystemActivity) -> None:
        self._reported_activity = activity
        if not self.settings.pause_on_display_off:
            activity = replace(activity, display_off=False)
        if self._stopped or activity == self.system_activity:
            return
        was_paused = self.system_activity.paused
        self.system_activity = activity
        if activity.paused and not was_paused:
            self._activity_epoch += 1
            if self.telemetry_export.running:
                self.telemetry_export.stop()
                self._last_export = None
                self._export_deadline = math.inf
            self._stop_mqtt()
            self._credential_cancelled.set()
            self._work_cancelled.set()
            self._timer.stop()
            if self._processes is not None:
                self._processes.stop()
            self._api_pending = False
            self._diagnostic_pending = False
            if "api" in self._active and self._api_mode == "devices" and self._discovery_pending is None:
                self._discovery_pending = (None,)
            self.credential_renewal_pending = False
            if self.mqtt_access_status == "mqtt_access_renewing":
                self.mqtt_access_status = ""
            self._resume_fields = set()
            self._resume_scans = {module.id for module in self.settings.integrations if module.enabled}
            for state in self.integration_states.values():
                state.gate.suspend()
                state.auto_reason = "automation_paused"
        self.system_activity_changed.emit(activity)
        self._activity_timer.start(0)

    def _apply_system_activity(self):
        if self._stopped:
            return
        if not self.system_activity.paused and self._work_cancelled.is_set():
            self._credential_cancelled = threading.Event()
            self._work_cancelled = threading.Event()
            self._configure_export()
            self._update_demand()
            self.polling.resume(self.clock())
            if self.connection not in {ConnectionState.AUTH_ERROR, ConnectionState.UNSUPPORTED}:
                if self.settings.telemetry_transport == "mqtt_with_https_fallback" and self._mqtt_retry_at < math.inf:
                    self._mqtt_retry_at = 0
                    self._mqtt_retry_delay = MQTT_RETRY_INITIAL
                    self._mqtt_fallback = True
                self._next_api = 0
                self._api_pending = bool(self.settings.serial)
            for state in self.integration_states.values():
                state.next_scan = 0
            if self._metadata_dirty:
                self._save_metadata()
            self._check_credential_expiry()
            self._continue_api()
            self.tick()
        self.changed.emit()

    @property
    def ui_settings(self):
        return self.preview if self.preview is not None else self.settings

    @property
    def fresh(self) -> bool:
        if self.polling.push_active:
            return self.fresh_for(CORE)
        keys = self.polling.required & CORE
        return self.fresh_for(keys or self.polling.required or CORE)

    def fresh_for(self, keys) -> bool:
        if self.system_activity.paused or self._resume_fields is not None and not set(keys) <= self._resume_fields:
            return False
        if self.connection not in {ConnectionState.LIVE, ConnectionState.STALE} or self.telemetry is None:
            return False
        if self.polling.samples:
            return self.polling.fresh(keys, self.clock()) if keys else True
        return self.connection == ConnectionState.LIVE and self.clock() - self.telemetry.received_monotonic < self.settings.polling.primary.stale_after_seconds

    def set_sensor_demand(self, source: str, keys) -> None:
        if frozenset(keys) == self._demands.get(source, frozenset()):
            return
        self._demands[source] = frozenset(keys)
        self._update_demand()
        self.wake()
        self.changed.emit()

    def _automation_sensors(self, module):
        keys = set()
        has_rule = False
        if module.enabled and module.auto_enabled:
            for rule in (*module.application_rules, *module.system_rules):
                if rule.enabled:
                    has_rule = True
                    if rule.input_condition != "any":
                        keys.add("input_power")
                    if rule.min_soc is not None or rule.max_soc is not None:
                        keys.add("battery_level")
        return frozenset(keys or ({"battery_level"} if has_rule else set()))

    def _update_demand(self):
        visible = frozenset(key for keys in self._demands.values() for metric in keys for key in dependencies(metric))
        required = set(visible)
        for module in self.settings.integrations:
            required.update(self._automation_sensors(module))
            if module.enabled:
                required.update(sensor for key in module.app_value_keys() for sensor in value_dependencies(key))
        if self.telemetry_export.running and self.settings.serial:
            profile = self.device_profile
            required.update(dict(profile.fields) if profile is not None else CORE)
        self.polling.supplemental = CORE | {"charging_state", "device_discharge_minutes", "device_charge_minutes"} if required else frozenset()
        if self.polling.set_demand(visible, required, self.clock()) and self.connection not in {ConnectionState.AUTH_ERROR, ConnectionState.API_ERROR, ConnectionState.NETWORK_UNAVAILABLE, ConnectionState.DEVICE_OFFLINE, ConnectionState.UNSUPPORTED}:
            self._next_api = self.polling.next_due(self.clock())

    def reading_state(self, key: str) -> str:
        if not self.polling.samples and self.telemetry is not None:
            return "ready" if self.fresh else "stale"
        if not self.settings.serial:
            return "missing"
        keys = dependencies(key)
        if key == "minimum_discharge_level" and self.minimum.source == "manual":
            return "ready"
        if key == "maximum_charge_level" and self.maximum.source == "manual":
            return "ready"
        if key == "estimated_runtime":
            keys = self.remaining_presentation_sensors
        if self.connection == ConnectionState.AUTH_ERROR and "api" not in self._active:
            return "stale" if any(self.polling.samples.get(sensor) is not None and self.polling.samples[sensor].value is not None for sensor in keys) else "missing"
        return self.polling.presentation(keys, self.clock())

    @property
    def motion_telemetry(self):
        if self.telemetry is None or not self.polling.samples:
            return self.telemetry
        names = {"input_power": "input_w", "output_power": "output_w", "battery_level": "soc_pct",
                 "charging_state": "charging_state", "ac_enabled": "ac_enabled", "dc_enabled": "dc_enabled"}
        return replace(self.telemetry, **{attribute: None for key, attribute in names.items() if not self.fresh_for({key})})

    @property
    def minimum(self):
        return resolve_limit(self.telemetry.min_soc if self.telemetry else None, self.settings.manual_min)

    @property
    def maximum(self):
        return resolve_limit(self.telemetry.max_soc if self.telemetry else None, self.settings.manual_max)

    @property
    def runtime(self):
        keys = CORE | ({"minimum_discharge_level"} if self.minimum.source != "manual" else set())
        return runtime_hours(self.telemetry, self.effective_capacity, self.minimum.value, self.fresh_for(keys))

    @property
    def charging(self):
        snapshot = self.telemetry
        return snapshot is not None and snapshot.input_w is not None and snapshot.output_w is not None and snapshot.input_w > snapshot.output_w

    @property
    def remaining_time_sensors(self):
        limit = self.maximum if self.charging else self.minimum
        key = "maximum_charge_level" if self.charging else "minimum_discharge_level"
        return CORE | ({key} if limit.source != "manual" else set())

    @property
    def remaining_presentation_sensors(self):
        if self.telemetry is not None and (dict(self.telemetry.ports).get("ac_input") or 0) > 0:
            return frozenset(("ac_input", "battery_level"))
        return self.remaining_time_sensors

    @property
    def remaining_time(self):
        if self.charging:
            return charge_hours(self.telemetry, self.effective_capacity, self.maximum.value, self.fresh_for(self.remaining_time_sensors))
        return self.runtime

    @property
    def remaining_presentation(self):
        from ecoflow_monitor.domain import present_remaining_time
        snapshot = self.telemetry
        return present_remaining_time(
            self.remaining_time, self.charging,
            dict(snapshot.ports).get("ac_input") if snapshot is not None else None,
            self.fresh_for({"ac_input"}), snapshot.soc_pct if snapshot is not None else None,
            self.fresh_for({"battery_level"}),
        )

    @property
    def device_name(self):
        return self.metadata.name(self.settings.serial) if self.settings.serial else i18n.t("device")

    @property
    def capacity_estimate(self):
        return self.metadata.sampler(self.settings.serial).estimate if self.settings.serial else None

    @property
    def effective_capacity(self):
        estimate = self.capacity_estimate
        return estimate.watt_hours if estimate is not None else self.settings.capacity_wh

    def _save_metadata(self):
        if self._stopped:
            return
        self._metadata_dirty = True
        if self.system_activity.paused:
            return
        if "metadata" not in self._active:
            data = self.metadata.snapshot()
            self._metadata_dirty = False
            self._submit("metadata", lambda: self.metadata.save(data))

    def module(self, integration_id: str) -> IntegrationDefinition:
        return next(module for module in self.settings.integrations if module.id == integration_id)

    def start(self, authorized=True) -> None:
        self._started = True
        self.access_allowed = authorized
        self._configure_export()
        event("application_started", version=__version__)
        try:
            self.credential_cache.initialize()
        except CredentialError as error:
            self.notice = str(error)
            self.connection = ConnectionState.AUTH_ERROR
            self._next_api = float("inf")
            self.changed.emit()
        self.tick()

    def _configure_export(self, reset=False):
        if self._started and (self.settings.telemetry_export.enabled or self.telemetry_export.running or self.telemetry_export.error):
            self.telemetry_export.configure(
                self.settings.telemetry_export, active=not self._stopped and not self.system_activity.paused,
                serial=self.settings.serial, device_name=self.device_name if self.settings.serial and self.access_allowed else "",
                reset=reset,
            )
        self._last_export = None
        self._export_deadline = math.inf

    def _update_export(self):
        if not self.telemetry_export.running:
            return
        now = self.clock()
        values, deadlines = app_readings(self)
        state = self.connection.value
        source = self.telemetry_source
        device_name = self.device_name if self.settings.serial and self.access_allowed else ""
        previous = self._last_export
        publication = (self.telemetry, values, deadlines, state, source, device_name, self.access_allowed)
        preferences = self.settings.telemetry_export
        if previous is None or previous[-1] != self.access_allowed or (preferences.mode == "updates" and (
            previous[0] is not self.telemetry or previous[1:] != publication[1:]
        )) or now >= self._export_deadline:
            self.telemetry_export.publish(state, source, values, deadlines, device_name, authorized=self.access_allowed)
            self._last_export = publication
            self._export_deadline = now + preferences.interval_seconds if preferences.mode == "interval" else math.inf
        elif preferences.mode == "interval":
            self.telemetry_export.invalidate(values, state)

    def wake(self):
        if self._started and not self._stopped and not self.system_activity.paused and self._timer.remainingTime() != 0:
            self._timer.start(0)

    def _submit(self, kind: str, operation: Callable) -> None:
        if self._stopped or self.system_activity.paused or kind in self._active:
            return
        cancelled = self._credential_cancelled if kind == "api" else self._work_cancelled if kind != "metadata" else None
        task = Task(kind, self._generation, operation, cancelled)
        task.activity_epoch = self._activity_epoch
        task.signals.finished.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._active[kind] = task
        self._pool.start(task)

    def discover(self, supplied: Credentials | None = None, source_settings=None, *, user_initiated=True) -> None:
        if self._stopped or self.system_activity.paused:
            return
        if user_initiated:
            self.access_allowed = True
        if not self.access_allowed:
            return
        if supplied is None and (source_settings is None or source_settings.credential_source == "1password") and self._check_credential_expiry():
            self.devices_ready.emit(())
            return
        if "api" in self._active:
            self._discovery_pending = (supplied, source_settings)
            return
        settings, cancelled = source_settings or self.settings, self._credential_cancelled
        self._discovery_snapshot = None
        event("device_discovery_started")
        self._api_mode = "devices"
        def operation():
            credentials = supplied if supplied is not None else self._read_credentials(settings, cancelled)
            if credentials is None:
                raise CredentialError("credentials_required")
            devices = self._client(credentials, cancelled, settings).devices()
            return ("devices", devices, supplied is None and settings == self.settings, DiscoverySnapshot(devices, settings, self._discovery_identity(credentials)))
        self._submit("api", operation)

    def refresh(self, *, user_initiated=True) -> None:
        if self._stopped or self.system_activity.paused:
            return
        if user_initiated:
            self.access_allowed = True
            if self.connection in {ConnectionState.AUTH_ERROR, ConnectionState.UNSUPPORTED}:
                self._reset_mqtt()
            self.wake()
        if not self.access_allowed:
            return
        if self._check_credential_expiry():
            return
        if "api" in self._active:
            self._api_pending = True
            return
        if not self.settings.serial:
            self.connection = ConnectionState.UNCONFIGURED
            self.changed.emit()
            return
        self._api_pending = False
        self._update_demand()
        if not self.polling.required:
            self._next_api = float("inf")
            return
        if self.clock() >= self._mqtt_probe_deadline():
            self._api_pending = self._mqtt_fallback and self.clock() >= self._next_api
            self._start_mqtt_setup()
            return
        if self._mqtt_session is not None and (self._mqtt_session.active or not self._mqtt_fallback):
            self._next_api = float("inf")
            return
        if self.connection in {ConnectionState.API_ERROR, ConnectionState.NETWORK_UNAVAILABLE, ConnectionState.DEVICE_OFFLINE}:
            self.polling.retry()
        if self.polling.samples and not self.polling.due(self.clock()):
            self._next_api = self.polling.next_due(self.clock())
            return
        event("telemetry_refresh_started")
        settings, cancelled = self.settings, self._credential_cancelled
        serial = settings.serial
        self._api_mode = "telemetry"
        polling = self.polling
        catalog = self.catalog
        profile_catalog = self.profile_catalog
        telemetry_epoch = self._telemetry_epoch
        generation = self._generation

        def raw_received(payload, secrets):
            self.telemetry_export.emit(
                "raw", {"source": "https", "payload": payload}, secrets=secrets,
                current=lambda: not cancelled.is_set() and self.access_allowed
                and generation == self._generation and self.settings.serial == serial
                and telemetry_epoch == self._telemetry_epoch,
            )

        def select_quotas():
            if cancelled.is_set():
                return ()
            quotas = polling.begin(self.clock())
            self.quotas_started.emit()
            event("telemetry_quotas_selected", level=logging.DEBUG, fields=list(quotas))
            return quotas
        def operation():
            credentials = self._read_credentials(settings, cancelled)
            if credentials is None:
                raise CredentialError("credentials_required")
            try:
                client = self._client(credentials, cancelled, settings, profile_catalog)
                if isinstance(client, EcoFlowClient):
                    client.raw_telemetry = raw_received
                    self._resolve_client_device(client, settings, catalog)
                if hasattr(client, "read_quotas"):
                    return ("telemetry", client.read_quotas(serial, select_quotas, verify_model=not polling.verified), getattr(client, "catalog", None), getattr(client, "profile_catalog", None))
                return ("telemetry", client.read(serial), getattr(client, "catalog", None))
            except EcoFlowError as error:
                if error.state == ConnectionState.API_ERROR and internet_unavailable():
                    raise EcoFlowError(ConnectionState.NETWORK_UNAVAILABLE) from None
                raise
        if self.telemetry is None:
            self.connection = ConnectionState.CONNECTING
        self._next_api = float("inf")
        self._submit("api", operation)
        if "api" in self._active:
            self._active["api"].polling = polling
            self._active["api"].telemetry_epoch = self._telemetry_epoch
        self.changed.emit()

    @staticmethod
    def _resolve_client_device(client, settings, catalog):
        client.catalog = catalog if catalog is not None else client.devices()
        device = next((item for item in client.catalog if item.serial == settings.serial), None)
        client.profile = client.profile_catalog.resolve(dict(settings.device_profile_assignments).get(settings.serial, ""),
                                                        device.model_id if device else "", device.name if device else "")
        return device

    @property
    def telemetry_source(self):
        if self._stopped or self.system_activity.paused or not self.access_allowed or not self.settings.serial or not self.polling.required:
            return ""
        if self.settings.telemetry_transport == "https_only":
            return "https"
        if self._mqtt_session is not None and self._mqtt_session.active:
            return "mqtt"
        if self._mqtt_fallback or self._mqtt_auth_blocked or self.clock() < self._mqtt_certification_retry_at:
            return "https_fallback"
        return "waiting_mqtt" if self._mqtt_session is not None else ""

    def _mqtt_probe_deadline(self):
        if (self.settings.telemetry_transport != "mqtt_with_https_fallback" or self._mqtt_session is not None
                or not self.polling.required or self._mqtt_auth_blocked):
            return math.inf
        return max(self._mqtt_retry_at, self._mqtt_certification_retry_at)

    @property
    def can_renew_mqtt_access(self) -> bool:
        return (self.settings.telemetry_transport == "mqtt_with_https_fallback" and bool(self.settings.serial)
                and self.access_allowed and not self._stopped and not self.system_activity.paused
                and not self.credential_renewal_required and self.connection not in {ConnectionState.AUTH_ERROR, ConnectionState.UNSUPPORTED}
                and "api" not in self._active and self.clock() >= self._mqtt_certification_retry_at)

    def renew_mqtt_access(self) -> None:
        if self.can_renew_mqtt_access and not self._check_credential_expiry():
            self.mqtt_access_status = "mqtt_access_renewing"
            self._start_mqtt_setup(renewal=True)

    def _start_mqtt_setup(self, *, renewal=False):
        settings, cancelled = self.settings, self._credential_cancelled
        session = MqttSession(settings.serial)
        if not renewal:
            self._mqtt_session = session
        catalog, profiles = self.catalog, self.profile_catalog
        cached = self._mqtt_access
        request_reason = "manual" if renewal else (
            "broker_rejected" if self.mqtt_access_status == "mqtt_access_rejected" else "initial")
        event("mqtt_setup_started", operation=session.operation)

        def operation():
            credentials = self._read_credentials(settings, cancelled)
            if credentials is None:
                raise CredentialError("credentials_required")
            client = self._client(credentials, cancelled, settings, profiles)
            if not isinstance(client, EcoFlowClient):
                return MqttSetup()
            device = self._resolve_client_device(client, settings, catalog)
            setup = MqttSetup(client.catalog, client.profile_catalog)
            if (device is None or client.profile is None or client.profile.reference != BASELINE
                    or not valid_topic_segment(settings.serial)):
                return setup
            if session.cancelled.is_set():
                raise ActivityCancelled()
            identity = self._discovery_identity(credentials)
            matching = cached if cached is not None and hmac.compare_digest(cached.credential_identity, identity) else None
            if matching is not None and not renewal:
                return replace(setup, access=matching, secrets=(credentials.access_key, credentials.secret_key))
            event("mqtt_access_requested", operation=session.operation, reason=request_reason)
            try:
                access = BrokerAccess.from_response(client.mqtt_certification())
            except (EcoFlowError, MqttError) as error:
                return replace(setup, access=matching, failure=error)
            return replace(setup, access=MqttAccess(identity, access), secrets=(credentials.access_key, credentials.secret_key))

        if not renewal and self.telemetry is None:
            self.connection = ConnectionState.CONNECTING
        self._api_mode = "mqtt_setup"
        self._submit("api", operation)
        self._active["api"].mqtt_session = session
        self._active["api"].mqtt_renewal = renewal
        self.changed.emit()

    def _finish_mqtt_setup(self, session, result: MqttSetup | None, error, *, renewal):
        if not renewal and session is not self._mqtt_session:
            return
        if renewal:
            self.mqtt_access_status = "mqtt_access_renew_failed"
        if renewal and error is not None:
            return
        auth_failed = isinstance(error, CredentialError) or isinstance(error, EcoFlowError) and error.state == ConnectionState.AUTH_ERROR
        if auth_failed:
            self._clear_mqtt_access()
            self._reset_mqtt()
            self._mqtt_retry_at = self._next_api = float("inf")
            self._api_pending = False
            self.connection = ConnectionState.AUTH_ERROR
            if isinstance(error, CredentialError):
                self.notice = str(error)
            elif self.settings.credential_source == "1password":
                try:
                    self.credential_cache.clear()
                except CredentialError as cache_error:
                    self.notice = str(cache_error)
        if self.credential_renewal_pending:
            self.credential_renewal_pending = False
            self.credential_renewal_required = not self.credential_cache.ready
        if auth_failed:
            return
        if error is not None:
            self._fallback_mqtt(error)
            return
        assert result is not None
        self._remember_catalog(result.catalog, result.profiles)
        if result.failure is not None:
            if result.access is None:
                self._mqtt_access = None
            if result.denied:
                self._mqtt_auth_blocked = True
            if not renewal:
                self._fallback_mqtt(result.failure, retry=not result.denied)
            return
        if result.access is None:
            if not renewal:
                self._fallback_mqtt(None, retry=False)
            return
        source = "cache" if result.access is self._mqtt_access else "api"
        event("mqtt_access_ready", operation=session.operation, source=source)
        self._mqtt_access = result.access
        if renewal:
            self._mqtt_auth_blocked = False
            self._mqtt_retry_at = 0
            self._mqtt_retry_delay = MQTT_RETRY_INITIAL
            self.mqtt_access_status = "mqtt_access_renewed"
            self._stop_mqtt()
            if not self.polling.required:
                return
            self._mqtt_session = session
            self._use_https_fallback(self.clock())
        elif self.mqtt_access_status == "mqtt_access_rejected":
            self.mqtt_access_status = ""
        self._start_mqtt_stream(session, result.access, result.secrets)

    def _start_mqtt_stream(self, session, cached, secrets):
        access = cached.broker
        session.reused_access = cached.attempted
        self._mqtt_access = replace(cached, attempted=True)
        session.deadline = self.clock() + 120

        def receive(patch):
            if session.offer(patch, time.time(), self.clock()):
                self.mqtt_readings_ready.emit(session)

        kind = f"mqtt:{id(session)}"
        def raw_received(payload):
            self.telemetry_export.emit(
                "raw", {"source": "mqtt", "payload": payload}, secrets=(*secrets, access.account, access.password),
                current=lambda: not session.cancelled.is_set() and self.access_allowed and session is self._mqtt_session
                and session.serial == self.settings.serial,
            )

        def operation():
            if self._mqtt_stream is stream_telemetry:
                return stream_telemetry(access, session.serial, session.cancelled, receive,
                                        raw_receive=raw_received, operation=session.operation)
            return self._mqtt_stream(access, session.serial, session.cancelled, receive)

        worker = Task(kind, self._generation, operation, session.cancelled)
        worker.mqtt_session = session
        worker.signals.finished.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._active[kind] = worker
        self._mqtt_pool.start(worker)

    def _accept_mqtt(self, session):
        if session is not self._mqtt_session or not self.access_allowed or self.system_activity.paused or self._check_credential_expiry():
            return
        pending = session.take()
        if not pending:
            return
        now = self.clock()
        if not session.active:
            if now >= session.deadline:
                self._fallback_mqtt(MqttError("first_report_timeout"))
                return
            session.readings.update(pending)
            if not session.core_ready(now, self.settings.polling.primary.stale_after_seconds):
                return
            pending, session.readings = session.readings, {}
            session.active = True
            self._telemetry_epoch += 1
            self._mqtt_fallback = False
            self._mqtt_retry_delay = MQTT_RETRY_INITIAL
            self._next_api = float("inf")
            self.polling.start_push()
            self._resume_fields = set()
            self.metadata.sampler(self.settings.serial).previous = None
            event("telemetry_source_changed", operation=session.operation, source="mqtt")
        self.polling.receive(pending)
        reports = {}
        for key, reading in pending.items():
            if reading.valid:
                reports.setdefault((reading.monotonic, reading.utc), {})[key] = reading.value
        for stamp, values in sorted(reports.items()):
            self._accept_sample_values(values, stamp[1], stamp[0])
        if reports:
            received, utc = max(reports)
            self.telemetry = self.polling.snapshot(self.telemetry, utc, received)
            self._clear_connection_notice()
        self.connection = ConnectionState.LIVE if self.polling.fresh(CORE, now) else ConnectionState.STALE
        self.wake()
        self.changed.emit()

    def _stop_mqtt(self):
        session, self._mqtt_session = self._mqtt_session, None
        if session is not None:
            session.cancelled.set()
            if session.active:
                self.polling.stop_push(self.clock())
                self._resume_fields = set()
                self.connection = ConnectionState.STALE
                self.metadata.sampler(session.serial).previous = None
                for state in self.integration_states.values():
                    state.gate.suspend()

    def _reset_mqtt(self):
        self._stop_mqtt()
        self._mqtt_retry_at = 0
        self._mqtt_retry_delay = MQTT_RETRY_INITIAL
        self._mqtt_fallback = False

    def _clear_mqtt_access(self):
        self._mqtt_access = None
        self._mqtt_auth_blocked = False
        self.mqtt_access_status = ""

    def _use_https_fallback(self, now):
        if not self._mqtt_fallback:
            self._mqtt_fallback = True
            self.polling.resume(now)
            self._next_api = 0
            self._api_pending = True

    def _fallback_mqtt(self, error, *, retry=True):
        session = self._mqtt_session
        reason = str(error) if isinstance(error, MqttError) else type(error).__name__ if error is not None else "profile_unavailable"
        if session is not None and reason == "broker_auth_rejected":
            self._mqtt_access = None
            self._mqtt_auth_blocked = not session.reused_access
            if self.mqtt_access_status != "mqtt_access_renewing":
                self.mqtt_access_status = "mqtt_access_rejected"
            retry = session.reused_access
        self._stop_mqtt()
        now = self.clock()
        delay = max(self._mqtt_retry_delay, getattr(error, "retry_after", None) or 0)
        self._mqtt_retry_at = now + delay if retry else float("inf")
        self._mqtt_retry_delay = min(2 * self._mqtt_retry_delay, MQTT_RETRY_MAX)
        self._use_https_fallback(now)
        event("mqtt_fallback", operation=session.operation if session is not None else None, reason=reason,
              retry_in_seconds=delay if retry else None)
        self.changed.emit()

    def _check_mqtt(self, now):
        session = self._mqtt_session
        if not self.access_allowed or not self.polling.required:
            if session is not None:
                self._reset_mqtt()
                self._next_api = float("inf")
                self.changed.emit()
            return
        if session is None:
            return
        if session.active and session.core_expired(now, self.settings.polling.primary.stale_after_seconds):
            self._fallback_mqtt(MqttError("core_readings_stale"))
        elif not session.active and now >= session.deadline:
            self._fallback_mqtt(MqttError("first_report_timeout"))

    def _read_credentials(self, settings: AppSettings, cancelled: threading.Event) -> Credentials | None:
        if cancelled.is_set():
            raise ActivityCancelled()
        event("credentials_requested", source=settings.credential_source)
        if settings.credential_source == "1password":
            return self.load_session_credentials(settings, cancelled)
        return self.credential_store.read(settings.credential_ref)

    def _client(self, credentials, cancelled, settings=None, profile_catalog=None):
        if cancelled.is_set():
            raise ActivityCancelled()
        client = self.client_factory(credentials)
        if isinstance(client, EcoFlowClient):
            client.cancelled = cancelled
            settings = settings or self.settings
            snapshot = profile_catalog if profile_catalog is not None else self.profile_catalog
            if snapshot is None or snapshot.custom_enabled != settings.custom_device_profiles:
                snapshot = load_catalog(self.profile_directory, settings.custom_device_profiles)
            client.profile_catalog = snapshot
            device = next((item for item in self.catalog or () if item.serial == settings.serial), None)
            client.profile = snapshot.resolve(dict(settings.device_profile_assignments).get(settings.serial, ""), device.model_id if device else "", device.name if device else "")
        return client

    def _discovery_identity(self, credentials):
        digest = hmac.new(self._discovery_key, digestmod="sha256")
        access = credentials.access_key.encode("utf-8")
        digest.update(len(access).to_bytes(8, "big"))
        digest.update(access)
        digest.update(credentials.secret_key.encode("utf-8"))
        return digest.digest()

    @property
    def profile_directory(self):
        return self.store.path.parent / "device-profiles"

    @property
    def device_profile(self):
        if self.profile_catalog is None:
            return None
        device = next((item for item in self.catalog or () if item.serial == self.settings.serial), None)
        return self.profile_catalog.resolve(dict(self.settings.device_profile_assignments).get(self.settings.serial, ""), device.model_id if device else "", device.name if device else "")

    def scan_profiles(self, enabled):
        if self._stopped or self.system_activity.paused or "profiles" in self._active:
            return False
        self._submit("profiles", lambda: load_catalog(self.profile_directory, enabled))
        return True

    @property
    def can_retry_credentials(self):
        return (self.settings.credential_source == "1password" and not self._stopped and not self.system_activity.paused
                and "api" not in self._active and not self.credential_renewal_pending
                and (self.credential_renewal_required or self.connection == ConnectionState.AUTH_ERROR
                     or self.notice.startswith("onepassword_") and self.notice not in {"onepassword_loaded", "onepassword_created"}))

    def load_session_credentials(self, settings, cancelled):
        identity = (settings.onepassword_access_ref, settings.onepassword_secret_ref)
        def load():
            operation = lambda: read_credentials(*identity, cancelled)
            return self.interactions.run_external(operation, cancelled) if self.interactions is not None else operation()
        if identity == (self.settings.onepassword_access_ref, self.settings.onepassword_secret_ref):
            credentials = self.credential_cache.get(identity, settings.onepassword_cache_ttl_seconds, cancelled, load)
            self.credential_cache_updated.emit()
            return credentials
        return load()

    def collect_diagnostics(self):
        if self._stopped or self.system_activity.paused or not self.settings.serial or self.diagnostics is None:
            return
        if "api" in self._active:
            self._diagnostic_pending = True
            return
        settings, cancelled = self.settings, self._credential_cancelled
        self.access_allowed = True
        self.notice = "device_diagnostics_running"
        def operation():
            credentials = self._read_credentials(settings, cancelled)
            if credentials is None:
                raise CredentialError("credentials_required")
            return ("diagnostic", self._client(credentials, cancelled).diagnose(settings.serial, self.diagnostics.capture))
        self._api_mode = "diagnostic"
        self._submit("api", operation)
        self.changed.emit()

    def _check_credential_expiry(self) -> bool:
        if self.settings.credential_source != "1password":
            return False
        if self.credential_renewal_pending:
            return False
        if self.credential_renewal_required:
            self._stop_mqtt()
            self._clear_mqtt_access()
            self._credential_cancelled.set()
            self._api_pending = False
            self._next_api = float("inf")
            self.connection = ConnectionState.AUTH_ERROR
            if not self.notice:
                self.notice = "onepassword_cache_expired"
            return True
        try:
            if not self.credential_cache.expire(self.settings.onepassword_cache_ttl_seconds):
                return False
            notice = "onepassword_cache_expired"
        except CredentialError as error:
            notice = str(error)
        self.credential_renewal_required = True
        self._stop_mqtt()
        self._clear_mqtt_access()
        event("credentials_expired", level=logging.WARNING)
        self._credential_cancelled.set()
        self._api_pending = False
        self._discovery_pending = None
        self._next_api = float("inf")
        self.connection = ConnectionState.AUTH_ERROR
        self.notice = notice
        self.credentials_expired.emit()
        self.changed.emit()
        return True

    def renew_credentials(self) -> None:
        if self._stopped or self.system_activity.paused or self.settings.credential_source != "1password" or "api" in self._active:
            return
        self.access_allowed = True
        self._clear_mqtt_access()
        self._reset_mqtt()
        self._credential_cancelled.set()
        event("credential_renewal_requested")
        self._credential_cancelled = threading.Event()
        try:
            self.credential_cache.clear()
        except CredentialError as error:
            self.notice = str(error)
            self.connection = ConnectionState.AUTH_ERROR
            self._next_api = float("inf")
            self.changed.emit()
            return
        self.credential_renewal_pending = True
        self.notice = ""
        if self.settings.serial and self.polling.required:
            self.polling.resume(self.clock())
            self.refresh()
        else:
            self.discover()
            self.changed.emit()

    def apply_settings(self, settings: AppSettings, credentials: Credentials | None = None, *, profile_catalog=None) -> None:
        settings.validate()
        validate_bindings(settings.hotkeys)
        if profile_catalog is not None and profile_catalog.custom_enabled != settings.custom_device_profiles:
            raise ValueError("device_profile_invalid")
        previous_profile_catalog = self.profile_catalog
        discovery = self._discovery_snapshot
        identity = self._discovery_identity(credentials) if credentials is not None else None
        discovered_devices = discovery.devices if discovery is not None and discovery.matches(settings, identity) else None
        write_local = credentials is not None and settings.credential_source == "windows"
        if write_local:
            settings = replace(settings, credential_ref="EcoFlowMonitor/" + uuid.uuid4().hex)
            self.credential_store.write(settings.credential_ref, credentials)
        try:
            self.store.save(settings, explicit=True)
        except SettingsError:
            event("settings_save_failed", level=logging.ERROR)
            if write_local and hasattr(self.credential_store, "delete"):
                try:
                    self.credential_store.delete(settings.credential_ref)
                except CredentialError:
                    logger.warning("An unused credential target could not be discarded")
            raise
        previous = self.settings
        self.settings = settings
        if previous.pause_on_display_off != settings.pause_on_display_off:
            self.set_system_activity(self._reported_activity)
        profile_preferences_changed = (previous.custom_device_profiles != settings.custom_device_profiles
                                       or previous.device_profile_assignments != settings.device_profile_assignments)
        if profile_catalog is not None:
            self.profile_catalog = profile_catalog
        elif previous.custom_device_profiles != settings.custom_device_profiles:
            self.profile_catalog = None if settings.custom_device_profiles else builtin_catalog()
        profile_changed = profile_preferences_changed or previous_profile_catalog != self.profile_catalog
        self.polling.configure(settings.polling)
        if self.diagnostics is not None:
            self.diagnostics.configure(settings.logging_mode)
        event("settings_saved")
        if not profile_changed and credentials is None and replace(previous, logging_mode=settings.logging_mode,
                polling=settings.polling, telemetry_export=settings.telemetry_export,
                pause_on_display_off=settings.pause_on_display_off) == settings:
            if previous.telemetry_export != settings.telemetry_export:
                self._configure_export()
            self._update_demand()
            if previous.polling != settings.polling and self.connection not in {ConnectionState.AUTH_ERROR, ConnectionState.API_ERROR, ConnectionState.NETWORK_UNAVAILABLE, ConnectionState.DEVICE_OFFLINE, ConnectionState.UNSUPPORTED}:
                self._next_api = self.polling.next_due(self.clock())
            self.preferences_changed.emit()
            self.changed.emit()
            self.wake()
            return
        credential_fields = ("serial", "credential_ref", "credential_source", "onepassword_access_ref", "onepassword_secret_ref")
        device_changed = profile_changed or any(getattr(previous, name) != getattr(settings, name) for name in credential_fields)
        credentials_changed = credentials is not None or any(
            getattr(previous, name) != getattr(settings, name) for name in credential_fields[1:])
        if credentials_changed:
            self._clear_mqtt_access()
        elif self.mqtt_access_status == "mqtt_access_renewing" or previous.telemetry_transport != settings.telemetry_transport:
            self.mqtt_access_status = ""
        session = self._mqtt_session
        if (device_changed or credentials_changed or previous.telemetry_transport != settings.telemetry_transport
                or session is not None and session.deadline == float("inf")):
            self._reset_mqtt()
        self._credential_cancelled.set()
        self._credential_cancelled = threading.Event()
        self.credential_renewal_pending = False
        i18n.setup(settings.language)
        if hasattr(self.integration_adapter, "retain_sessions"):
            self.integration_adapter.retain_sessions(settings.integrations)
        self._generation += 1
        self._discovery_pending = None
        self._diagnostic_pending = False
        if device_changed:
            self.telemetry = None
            self.polling = PollingState(settings.polling)
            self.connection = ConnectionState.CONNECTING if settings.serial else ConnectionState.UNCONFIGURED
        if (device_changed or previous.telemetry_export != settings.telemetry_export
                or previous.telemetry_transport != settings.telemetry_transport):
            self._configure_export(reset=device_changed or previous.telemetry_transport != settings.telemetry_transport)
        if profile_changed:
            if settings.serial:
                self.metadata.sampler(settings.serial).previous = None
            for state in self.integration_states.values():
                state.gate.suspend()
        self._next_api = float("inf") if self.polling.push_active else 0
        self._update_demand()
        self._api_pending = bool(settings.serial and not self.polling.push_active)
        self._retry = 5
        self.notice = ""
        cache_fields = ("credential_source", "onepassword_access_ref", "onepassword_secret_ref")
        if any(getattr(previous, name) != getattr(settings, name) for name in cache_fields):
            self.catalog = None
            try:
                self.credential_cache.clear()
                self.credential_renewal_required = False
            except CredentialError as error:
                self.notice = str(error)
                self.credential_renewal_required = settings.credential_source == "1password"
                self.connection = ConnectionState.AUTH_ERROR
                self._next_api = float("inf")
                self._api_pending = False
        if previous.credential_ref != settings.credential_ref:
            self.catalog = None
        if discovered_devices is not None:
            self.catalog = discovered_devices
            self.metadata.remember(discovered_devices)
            self._save_metadata()
        self._discovery_snapshot = None
        if credentials is not None and settings.credential_source == "1password" and not self.credential_renewal_required:
            identity = (settings.onepassword_access_ref, settings.onepassword_secret_ref)
            try:
                self.credential_cache.adopt(identity, credentials)
            except CredentialError as error:
                self.notice = str(error)
                self.credential_renewal_required = True
        self._check_credential_expiry()
        previous_modules = {module.id: module for module in previous.integrations}
        self.integration_cache.configure(settings.integrations)
        self.integration_states = {
            module.id: self.integration_states[module.id]
            if module.id in previous_modules and previous_modules[module.id].enabled == module.enabled
            and command_fingerprint(previous_modules[module.id]) == command_fingerprint(module) else IntegrationState()
            for module in settings.integrations
        }
        for module in settings.integrations:
            state = self.integration_states[module.id]
            if state.busy:
                state.busy = False
                state.active_profile = None
                state.result = TransitionResult(False, error="integration_cancelled")
            if not module.enabled:
                self.integration_states[module.id].observation = IntegrationObservation("integration_disabled")
        self.preferences_changed.emit()
        self.changed.emit()

    def update_widget(self, *, overlay_fields: tuple[str, ...] = (), **changes) -> None:
        self.update_preferences(widget=replace(self.settings.widget, **changes), widget_fields=tuple(changes), widget_overlay_fields=overlay_fields)

    def update_preferences(self, *, mini_overlay_fields: tuple[str, ...] = (), main_overlay_fields: tuple[str, ...] = (), widget_fields: tuple[str, ...] = (), widget_overlay_fields: tuple[str, ...] = (), **changes) -> None:
        previous = self.settings
        settings = replace(previous, **changes)
        overlay_edits = {}
        for kind, explicit in (("mini", mini_overlay_fields), ("main", main_overlay_fields)):
            key = kind + "_overlay"
            if key in changes:
                current, old = getattr(settings, key), getattr(previous, key)
                overlay_edits[kind] = {name: getattr(current, name) for name in current.__dataclass_fields__ if name in explicit or getattr(old, name) != getattr(current, name)}
        try:
            self.store.save(settings)
        except SettingsError as error:
            self.notice = str(error)
        self.settings = settings
        if previous.pause_on_display_off != settings.pause_on_display_off:
            self.set_system_activity(self._reported_activity)
        if "integrations" in changes:
            self.integration_cache.configure(settings.integrations)
        self._update_demand()
        if "integrations" in changes and hasattr(self.integration_adapter, "retain_sessions"):
            self.integration_adapter.retain_sessions(settings.integrations)
        if self.preview is not None:
            immediate = {name: value for name, value in changes.items() if name in {"mini_enabled", "main_enabled", "mini_position", "monitor_geometry"}}
            if "widget" in changes:
                fields = widget_fields or tuple(name for name in settings.widget.__dataclass_fields__ if getattr(previous.widget, name) != getattr(settings.widget, name))
                edits = {name: getattr(settings.widget, name) for name in fields}
                if "overlay" in edits:
                    overlay_names = widget_overlay_fields or tuple(name for name in settings.widget.overlay.__dataclass_fields__ if getattr(previous.widget.overlay, name) != getattr(settings.widget.overlay, name))
                    edits["overlay"] = replace(self.preview.widget.overlay, **{name: getattr(settings.widget.overlay, name) for name in overlay_names})
                immediate["widget"] = replace(self.preview.widget, **edits)
            for kind, edits in overlay_edits.items():
                immediate[kind + "_overlay"] = replace(getattr(self.preview, kind + "_overlay"), **edits)
            self.preview = replace(self.preview, **immediate)
        self.runtime_preferences_changed.emit({**changes, "widget_fields": widget_fields, "widget_overlay_fields": widget_overlay_fields, **{kind + "_overlay_fields": tuple(overlay_edits.get(kind, {})) for kind in ("main", "mini")}})
        geometry_only = set(changes) == {"widget"} and set(widget_fields) <= {"geometry", "overlay"} and not widget_overlay_fields and settings.widget.overlay == previous.widget.overlay
        if not geometry_only and set(changes) - {"monitor_geometry", "mini_position", "monitor_visible"}:
            self.preferences_changed.emit()
        self.changed.emit()

    def set_auto(self, integration_id: str, enabled: bool) -> None:
        module = self.module(integration_id)
        if not module.enabled:
            return
        state = self.integration_states[integration_id]
        state.gate.reset()
        self.wake()
        if not state.busy:
            state.result = None
        self.update_preferences(integrations=tuple(replace(item, auto_enabled=enabled) if item.id == integration_id else item for item in self.settings.integrations))

    def request_profile(self, integration_id: str, profile_id: str, automatic: bool = False) -> None:
        if self._stopped or self.system_activity.paused:
            return
        self.integration_cache.configure(self.settings.integrations)
        module = self.module(integration_id)
        module_index = self.settings.integrations.index(module) + 1
        event("profile_requested", module_index=module_index, automatic=automatic)
        if not automatic:
            self.set_auto(integration_id, False)
            module = self.module(integration_id)
        state = self.integration_states[integration_id]
        kind = "dispatch:" + integration_id
        target = next((profile for profile in module.profiles if profile.id == profile_id), None)
        if not module.enabled or not state.observation.ready or kind in self._active or target is None:
            event("profile_request_rejected", level=logging.WARNING, module_index=module_index, reason="unavailable")
            self.notice = "profile_unavailable"
            self.changed.emit()
            return
        manual_retry = not automatic and module.driver == "afterburner"
        if state.active_profile == profile_id and not manual_retry and (state.result is None or state.result.success):
            return
        previous_id = state.active_profile or state.transition_from
        previous = next((profile for profile in module.profiles if profile.id == previous_id), None)
        keys = reaction_value_keys(target.activation, *((previous.deactivation,) if previous is not None else ()))
        values = {}
        if keys:
            readings, _ = app_readings(self)
            if not keys <= readings.keys():
                if not automatic:
                    self.notice = "integration_values_unavailable"
                    self.changed.emit()
                return
            values = {key: readings[key] for key in keys}
        state.transition_from = previous_id
        generation = self._generation
        activity_epoch = self._activity_epoch
        state.busy = True
        state.dispatch_revision += 1
        state.requested_profile = profile_id
        state.result = None
        def current():
            return (not self._stopped and not self.system_activity.paused and generation == self._generation
                    and activity_epoch == self._activity_epoch and self.module(integration_id).enabled)

        def report(profile, phase, step, result):
            self.telemetry_export.emit("commands", {
                "dispatch_id": dispatch_id, "integration_id": module.id, "profile_id": profile,
                "phase": phase, "step": step, "trigger": "auto" if automatic else "manual",
                "event": "started" if result is None else "finished",
                **({} if result is None else {"success": result.success, "error": result.error or None,
                                             "exit_code": result.exit_code, "winerror": result.winerror}),
            }, current=current)

        log_commands = self.telemetry_export.running and self.settings.telemetry_export.commands
        dispatch_id = uuid.uuid4().hex if log_commands else ""

        def operation(target_profile: IntegrationProfile = target):
            if not current():
                return profile_id, automatic, TransitionResult(False, error="integration_cancelled")
            try:
                revision = self.integration_cache.begin(module)
            except OSError:
                logger.warning("Integration state could not be invalidated before dispatch")
                return profile_id, automatic, TransitionResult(False, error="integration_command_failed")
            if revision is None or not current():
                return profile_id, automatic, TransitionResult(False, error="integration_cancelled")
            options = {"values": values} if values else {}
            if log_commands and isinstance(self.integration_adapter, IntegrationAdapter):
                options["report"] = report
            outcome = self.integration_adapter.transition(module, previous, target_profile, current, **options)
            if outcome.success:
                self.integration_cache.finish(module, revision, profile_id, outcome.process)
            return profile_id, automatic, outcome
        self._submit(kind, operation)
        if kind in self._active:
            self._active[kind].integration_state = state
        self.changed.emit()

    def tick(self) -> None:
        if self._stopped or self.system_activity.paused:
            return
        self._timer.stop()
        credentials_expired = self._check_credential_expiry()
        if self.integration_cache.dirty:
            self._submit("integration_cache", self.integration_cache.flush)
        now = self.clock()
        if 0 < self._mqtt_certification_retry_at <= now:
            self._mqtt_certification_retry_at = 0
            self.changed.emit()
        self._update_demand()
        self._check_mqtt(now)
        revision = (self.polling.revision, tuple((key, self.reading_state(key)) for key in self.polling.visible))
        if revision != self._polling_revision:
            self._polling_revision = revision
            self.changed.emit()
        if self.polling.required and self.connection == ConnectionState.LIVE and not self.fresh:
            self.connection = ConnectionState.STALE
            event("connection_changed", state=self.connection.value)
            self.changed.emit()
        elif self.connection == ConnectionState.STALE and self.fresh:
            self.connection = ConnectionState.LIVE
            self.changed.emit()
        if not credentials_expired and now >= self._api_deadline():
            self.refresh(user_initiated=False)
        process_scan = self._refresh_integration_observations(now)
        automation_due = math.inf
        for module in self.settings.integrations:
            state = self.integration_states[module.id]
            if process_scan and module.enabled:
                continue
            eligible = (self.access_allowed and module.enabled and module.auto_enabled and module.id not in self._resume_scans
                        and self.fresh_for(self._automation_sensors(module)) and state.observation.ready)
            reason = "automatic_mode" if eligible else "automation_paused"
            if reason != state.auto_reason:
                state.auto_reason = reason
                event("automation_state_changed", state=reason)
                self.changed.emit()
            if not eligible:
                state.gate.suspend()
                continue
            if state.busy:
                continue
            rule = module.select(self.telemetry, state.observation.running)
            profile_id = state.gate.evaluate(rule.profile_id if rule else None, now, state.active_profile)
            if profile_id is not None:
                self.request_profile(module.id, profile_id, automatic=True)
            else:
                due = state.gate.next_evaluation(state.active_profile)
                if due > now:
                    automation_due = min(automation_due, due)
        self._arm_next_tick(now, automation_due)

    def _api_deadline(self):
        if (not self.access_allowed or not self.settings.serial or not self.polling.required
                or self.credential_renewal_required or "api" in self._active
                or self.connection in {ConnectionState.AUTH_ERROR, ConnectionState.UNSUPPORTED}
                and not self.credential_renewal_pending):
            return math.inf
        return min(self._next_api, self._mqtt_probe_deadline())

    def _arm_next_tick(self, now, automation_due):
        if not self._started or self._stopped or self.system_activity.paused:
            return
        session = self._mqtt_session
        mqtt_due = math.inf if session is None else (
            session.core_deadline(self.settings.polling.primary.stale_after_seconds) if session.active else session.deadline)
        scan_due = self._integration_scan_deadline()
        cache_due = math.inf
        if self.settings.credential_source == "1password" and not self.credential_renewal_pending and not self.credential_renewal_required:
            cache_due = self.clock() + self.credential_cache.seconds_until_expiry(self.settings.onepassword_cache_ttl_seconds)
        stale_due = self.polling.next_stale(self.polling.required, now)
        if not self.polling.samples and self.telemetry is not None and self.fresh:
            stale_due = self.telemetry.received_monotonic + self.settings.polling.primary.stale_after_seconds
        self._update_export()
        certification_due = self._mqtt_certification_retry_at if self._mqtt_certification_retry_at > now else math.inf
        deadline = min(automation_due, mqtt_due, scan_due, cache_due, certification_due,
                       self._api_deadline(), stale_due, self._export_deadline)
        if deadline < math.inf:
            delay = min(2_147_483_647, max(0, deadline - self.clock()) * 1000)
            self._timer.start(math.ceil(delay))

    def _refresh_integration_observations(self, now):
        modules = tuple(module for module in self.settings.integrations if module.enabled)
        tracker = self._processes
        snapshot = revision = None
        if tracker is not None:
            tracker.configure(modules)
            tracker.advance(now)
            if "process_snapshot" not in self._active and (token := tracker.begin_snapshot(now)) is not None:
                def seeds():
                    return relevant_processes(integration_process_names(modules))

                self._submit("process_snapshot", seeds)
                self._active["process_snapshot"].snapshot_token = token
            if not tracker.initialized:
                return True
            revision = tracker.revision
            due = revision != self._inspected_process_revision or any(self.integration_states[module.id].next_scan == 0 for module in modules)
            if not due or "scan_all" in self._active:
                return due
            snapshot = tracker.snapshot()
        elif "scan_all" in self._active or not any(now >= self.integration_states[module.id].next_scan for module in modules):
            return False

        requests = tuple((module, self.integration_states[module.id].dispatch_revision) for module in modules)
        for module in modules:
            self.integration_states[module.id].next_scan = now + 2

        def inspect_all():
            shared = snapshot
            if shared is None and self._shared_process_snapshot:
                shared = running_processes()
            outcomes = {}
            for module, dispatch_revision in requests:
                try:
                    outcomes[module.id] = self._inspect_integration(module, dispatch_revision, shared), None
                except Exception as error:
                    outcomes[module.id] = None, error
            return revision, outcomes

        self._submit("scan_all", inspect_all)
        return tracker is not None

    def _integration_scan_deadline(self):
        if self._processes is not None:
            return self._processes.deadline(snapshot_busy="process_snapshot" in self._active)
        if "scan_all" in self._active:
            return math.inf
        return min((self.integration_states[module.id].next_scan for module in self.settings.integrations if module.enabled),
                   default=math.inf)

    def _inspect_integration(self, module, revision, snapshot=None):
        observation = self.integration_adapter.inspect(module) if snapshot is None else self.integration_adapter.inspect(module, snapshot)
        restored = self.integration_cache.restore(module, observation.process) if observation.ready else None
        return observation, restored, revision

    def _remember_catalog(self, catalog, profiles):
        if profiles is not None:
            self.profile_catalog = profiles
        if catalog is not None and self.catalog is None:
            self.catalog = catalog
            self.metadata.remember(catalog)
            self._save_metadata()

    def _accept_sample_values(self, values, utc, now):
        if self._resume_fields is not None:
            self._resume_fields.update(values)
        if self.metadata.sampler(self.settings.serial).observe(values, utc, now, self.settings.polling.primary.stale_after_seconds):
            self._save_metadata()

    def _clear_connection_notice(self):
        if self.notice.startswith("onepassword_") or self.notice in {"credentials_required", "credentials_error", *(state.value for state in ConnectionState)}:
            self.notice = ""

    def _completed(self, kind, generation, result, error) -> None:
        try:
            self._accept_completion(kind, generation, result, error)
        finally:
            self.wake()

    def _accept_completion(self, kind, generation, result, error) -> None:
        previous_connection = self.connection
        task = self._active.pop(kind, None)
        if kind.startswith("elevate:"):
            if hasattr(self.integration_adapter, "retain_sessions"):
                self.integration_adapter.retain_sessions(() if self._stopped else self.settings.integrations)
        if self._stopped:
            self.task_finished.emit(kind)
            return
        if kind == "process_snapshot":
            accepted = self._processes.finish_snapshot(task.snapshot_token, result if error is None else None)
            if accepted and error is not None:
                for module in self.settings.integrations:
                    if module.enabled:
                        self._finish_scan(module.id, None, error)
                self.changed.emit()
            return
        if kind.startswith("mqtt:"):
            if task.mqtt_session is self._mqtt_session:
                self._fallback_mqtt(error or MqttError("stream_stopped"))
                self._continue_api()
            return
        if kind in {"profiles", "profile_example"}:
            if error is not None:
                event("device_profile_operation_failed", level=logging.WARNING, operation=kind, exception_type=type(error).__name__)
            if kind == "profiles":
                self.profiles_ready.emit(result)
            else:
                self.profile_example_ready.emit(result, error)
            self.task_finished.emit(kind)
            return
        if kind == "metadata" and self._metadata_dirty:
            self._save_metadata()
        if kind == "api":
            if not self.system_activity.paused:
                self._check_credential_expiry()
            if (generation != self._generation or task is not None and task.cancelled is not None and task.cancelled.is_set()) and getattr(task, "polling", None) is self.polling:
                self.polling.abandon()
            session = getattr(task, "mqtt_session", None)
            if session is not None or getattr(task, "telemetry_epoch", self._telemetry_epoch) != self._telemetry_epoch:
                if session is not None:
                    retry_after = getattr(result.failure, "retry_after", None) if isinstance(result, MqttSetup) else None
                    if retry_after is not None and 0 < retry_after < math.inf:
                        self._mqtt_certification_retry_at = max(
                            self._mqtt_certification_retry_at, self.clock() + retry_after)
                    if generation == self._generation and not task.cancelled.is_set() and self.access_allowed:
                        self._finish_mqtt_setup(session, result, error, renewal=task.mqtt_renewal)
                self._continue_api()
                self.changed.emit()
                self.task_finished.emit(kind)
                return
        interrupted = task is not None and getattr(task, "activity_epoch", self._activity_epoch) != self._activity_epoch
        if interrupted and kind.startswith("dispatch:") and generation == self._generation:
            self._finish_dispatch(kind, result, error, task, interrupted=True)
        if generation == self._generation and not (task is not None and task.cancelled is not None and task.cancelled.is_set()):
            if kind == "api":
                if error is not None:
                    state = error.state if isinstance(error, EcoFlowError) else ConnectionState.AUTH_ERROR if isinstance(error, CredentialError) else ConnectionState.API_ERROR
                    event("api_operation_failed", level=logging.WARNING, state=state.value, operation=self._api_mode,
                          exception_type=type(error).__name__)
                    if isinstance(error, CredentialError):
                        self.notice = str(error)
                        if self._api_mode == "devices":
                            self._api_pending = False
                            if not self.fresh:
                                self.access_allowed = False
                    elif self._api_mode == "devices":
                        self.notice = state.value
                    if self._api_mode == "diagnostic":
                        self.notice = "device_diagnostics_failed"
                    elif self._api_mode != "devices":
                        self.polling.fail(self.clock())
                        self.connection = state
                        if state == ConnectionState.AUTH_ERROR:
                            self._clear_mqtt_access()
                            self._reset_mqtt()
                            self._mqtt_retry_at = float("inf")
                        if state == ConnectionState.UNSUPPORTED:
                            self._next_api = float("inf")
                            if self.settings.serial not in self._unsupported_notified:
                                self._unsupported_notified.add(self.settings.serial)
                                self.unsupported_device.emit()
                        self._next_api = float("inf") if self.connection in {ConnectionState.AUTH_ERROR, ConnectionState.UNSUPPORTED} else self.clock() + max(self._retry, getattr(error, "retry_after", None) or 0)
                        self._retry = min(self._retry * 2, 300)
                        event("api_retry_scheduled", level=logging.DEBUG, delay_seconds=None if self._next_api == float("inf") else max(0, self._next_api - self.clock()))
                    if self.settings.credential_source == "1password" and isinstance(error, EcoFlowError) and error.state == ConnectionState.AUTH_ERROR:
                        try:
                            self.credential_cache.clear()
                        except CredentialError as cache_error:
                            self.notice = str(cache_error)
                    if self._api_mode == "devices":
                        self.devices_ready.emit(())
                elif result[0] == "devices":
                    event("device_discovery_completed", count=len(result[1]))
                    self._discovery_snapshot = result[3] if len(result) > 3 else None
                    if len(result) < 3 or result[2]:
                        self.catalog = result[1]
                        self.metadata.remember(result[1])
                        self._save_metadata()
                    self.devices_ready.emit(result[1])
                elif result[0] == "diagnostic":
                    self._diagnostic_failures = result[1]
                    self.notice = "device_diagnostics_partial" if result[1] else "device_diagnostics_complete"
                else:
                    event("telemetry_refresh_completed")
                    self._remember_catalog(result[2] if len(result) > 2 else None, result[3] if len(result) > 3 else None)
                    if isinstance(result[1], TelemetryPatch):
                        patch = result[1]
                        now, utc = self.clock(), time.time()
                        self.polling.complete(patch, utc, now)
                        self._accept_sample_values(dict(patch.values), utc, now)
                        if patch.values:
                            self.telemetry = self.polling.snapshot(self.telemetry, utc, now)
                            self.connection = ConnectionState.LIVE
                        self._next_api = self.polling.next_due(now)
                    else:
                        self.telemetry = result[1]
                        if self._resume_fields is not None:
                            self._resume_fields.update(SENSOR_FIELDS)
                        self.connection = ConnectionState.LIVE
                        self._next_api = self.clock() + self.settings.polling.primary.interval_seconds
                    self._retry = 5
                    self._clear_connection_notice()
                if self.credential_renewal_pending:
                    self.credential_renewal_pending = False
                    self.credential_renewal_required = not self.credential_cache.ready
            elif kind == "scan_all":
                process_revision, outcomes = result if error is None else (None, {
                    module.id: (None, error) for module in self.settings.integrations if module.enabled})
                if self._processes is None or process_revision == self._processes.revision:
                    self._inspected_process_revision = process_revision
                    for integration_id, (observation, failure) in outcomes.items():
                        self._finish_scan(integration_id, observation, failure)
            elif kind.startswith("elevate:"):
                self.notice = "integration_helper_failed" if error else "integration_helper_ready"
            elif kind.startswith("elevation_check:"):
                if result is True and error is None:
                    self.elevation_needed.emit(kind.split(":", 1)[1])
            elif kind.startswith("dispatch:"):
                self._finish_dispatch(kind, result, error, task)
            if error is not None and not isinstance(error, (EcoFlowError, CredentialError)):
                logger.error("Background %s failed (%s)", kind.split(":", 1)[0], type(error).__name__)
            if previous_connection != self.connection:
                event("connection_changed", state=self.connection.value)
            self.changed.emit()
        if kind == "api" and not self.system_activity.paused:
            self._continue_api()
            self.changed.emit()
        self.task_finished.emit(kind)

    def _finish_scan(self, integration_id, result, error):
        state = self.integration_states[integration_id]
        previous = state.observation
        observation, restored, revision = result if error is None else (IntegrationObservation("integration_error"), None, state.dispatch_revision)
        state.observation = observation
        if not state.busy and revision == state.dispatch_revision:
            previous_target = state.active_process or previous.process
            if previous_target is not None and previous_target != observation.process:
                state.active_profile = None
                state.active_process = None
                state.transition_from = None
                state.gate.suspend()
            if restored is not None and (state.result is None or state.result.success or state.result.error == "integration_cancelled"):
                state.active_profile = restored
                state.active_process = observation.process
        if error is None:
            self._resume_scans.discard(integration_id)
        else:
            logger.error("Integration observation failed (%s)", type(error).__name__)
        if previous.availability != observation.availability:
            event("integration_availability_changed", module_index=self.settings.integrations.index(self.module(integration_id)) + 1,
                  state=observation.availability)

    def _finish_dispatch(self, kind, result, error, task, interrupted=False):
        integration_id = kind.split(":", 1)[1]
        state = self.integration_states[integration_id]
        if task is not None and getattr(task, "integration_state", state) is not state:
            return
        profile_id, automatic, outcome = result if error is None else (
            state.requested_profile, True, TransitionResult(False, error="integration_cancelled" if interrupted else "integration_command_failed"))
        state.busy = False
        cancelled = interrupted and outcome.error == "integration_cancelled"
        state.result = None if cancelled else outcome
        event("profile_operation_completed", level=logging.INFO if outcome.success or cancelled else logging.WARNING,
              module_index=self.settings.integrations.index(self.module(integration_id)) + 1,
              success=outcome.success, completed_commands=outcome.completed,
              exit_code=outcome.exit_code, winerror=outcome.winerror, reason=outcome.error)
        if outcome.success:
            state.active_profile = profile_id
            state.active_process = outcome.process
            state.transition_from = None
        elif not cancelled or outcome.completed:
            state.active_profile = None
            state.active_process = None
            if interrupted:
                state.transition_from = None
        if interrupted:
            state.requested_profile = None
            state.gate.suspend()
        elif automatic:
            state.gate.dispatched(profile_id, self.clock(), outcome.success)
        if not outcome.success and not cancelled:
            self.notice = "profile_failed"
            if not self.system_activity.paused and outcome.winerror in (5, 740):
                self.elevation_needed.emit(integration_id)
        elif self.notice in {"profile_failed", "profile_unavailable", "integration_values_unavailable"}:
            self.notice = ""
        state.next_scan = 0

    def _continue_api(self):
        if self._stopped or self.system_activity.paused or "api" in self._active:
            return
        if self._discovery_pending is not None:
            pending = self._discovery_pending
            self._discovery_pending = None
            self.discover(*pending, user_initiated=False)
        elif self._diagnostic_pending:
            self._diagnostic_pending = False
            self.collect_diagnostics()
        elif self._api_pending:
            self._api_pending = False
            self.refresh(user_initiated=False)

    def discard_discovery(self):
        self._discovery_snapshot = None

    def stop(self) -> None:
        if self._stopped:
            return
        event("application_stopping")
        self._stopped = True
        if self.telemetry_export.running:
            self.telemetry_export.stop()
        if self._processes is not None:
            self._processes.stop()
        self._stop_mqtt()
        self._clear_mqtt_access()
        self._discovery_snapshot = None
        self._credential_cancelled.set()
        self._work_cancelled.set()
        try:
            self.credential_cache.close()
        except CredentialError:
            logger.error("The temporary 1Password credential record could not be removed")
        self._generation += 1
        self._timer.stop()
        self._activity_timer.stop()
        self._pool.clear()
        self._mqtt_pool.clear()
        if hasattr(self.integration_adapter, "close"):
            self.integration_adapter.close()
        self._pool.waitForDone(22000)
        self._mqtt_pool.waitForDone(6000)
        self.integration_cache.flush()
        if self.metadata.devices or self.metadata.samplers:
            self.metadata.save()
        if hasattr(self.integration_adapter, "close"):
            self.integration_adapter.close()
        event("application_stopped")

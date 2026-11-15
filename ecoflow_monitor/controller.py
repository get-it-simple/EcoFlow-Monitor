from dataclasses import dataclass, field, replace
import hmac
import logging
import secrets
import threading
import time
import uuid
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal

from ecoflow_monitor import __version__, i18n
from ecoflow_monitor.diagnostics import DiagnosticLog, event
from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation, TransitionResult
from ecoflow_monitor.adapters.credentials import CredentialError, CredentialStore
from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError
from ecoflow_monitor.adapters.onepassword import read_credentials
from ecoflow_monitor.adapters.hotkeys import validate_bindings
from ecoflow_monitor.adapters.windows import internet_unavailable
from ecoflow_monitor.credential_cache import SessionCredentialCache
from ecoflow_monitor.device_metadata import DeviceMetadataStore
from ecoflow_monitor.device_profiles import builtin_catalog, load_catalog
from ecoflow_monitor.domain import AutomationGate, ConnectionState, charge_hours, resolve_limit, runtime_hours
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile
from ecoflow_monitor.settings import AppSettings, SettingsError, SettingsStore
from ecoflow_monitor.polling import CORE, DASHBOARD_SENSORS, SENSOR_FIELDS, PollingState, TelemetryPatch, dependencies
from ecoflow_monitor.system_activity import ActivityCancelled, SystemActivity


logger = logging.getLogger(__name__)


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
    requested_profile: str | None = None
    transition_from: str | None = None
    result: TransitionResult | None = None
    busy: bool = False
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

    def __init__(self, store: SettingsStore, credential_store=None, integration_adapter=None, client_factory=None, clock=time.monotonic, credential_cache=None, diagnostics: DiagnosticLog | None = None) -> None:
        super().__init__()
        self.store = store
        self.credential_store = credential_store if credential_store is not None else CredentialStore()
        self.integration_adapter = integration_adapter if integration_adapter is not None else IntegrationAdapter()
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
        self.preview = None
        self.system_activity = SystemActivity()
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
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.tick)
        self._activity_timer = QTimer(self)
        self._activity_timer.setSingleShot(True)
        self._activity_timer.timeout.connect(self._apply_system_activity)

    def set_system_activity(self, activity: SystemActivity) -> None:
        if self._stopped or activity == self.system_activity:
            return
        was_paused = self.system_activity.paused
        self.system_activity = activity
        if activity.paused and not was_paused:
            self._activity_epoch += 1
            self._credential_cancelled.set()
            self._work_cancelled.set()
            self._timer.stop()
            self._api_pending = False
            self._diagnostic_pending = False
            if "api" in self._active and self._api_mode == "devices" and self._discovery_pending is None:
                self._discovery_pending = (None,)
            self.credential_renewal_pending = False
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
        if not self.system_activity.paused:
            self._credential_cancelled = threading.Event()
            self._work_cancelled = threading.Event()
            self._update_demand()
            self.polling.resume(self.clock())
            if self.connection not in {ConnectionState.AUTH_ERROR, ConnectionState.UNSUPPORTED}:
                self._next_api = 0
                self._api_pending = bool(self.settings.serial)
            for state in self.integration_states.values():
                state.next_scan = 0
            if self._started:
                self._timer.start()
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
            keys = self.remaining_time_sensors
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
    def remaining_time(self):
        if self.charging:
            return charge_hours(self.telemetry, self.effective_capacity, self.maximum.value, self.fresh_for(self.remaining_time_sensors))
        return self.runtime

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
        event("application_started", version=__version__)
        try:
            self.credential_cache.initialize()
        except CredentialError as error:
            self.notice = str(error)
            self.connection = ConnectionState.AUTH_ERROR
            self._next_api = float("inf")
            self.changed.emit()
        if not self.system_activity.paused:
            self._timer.start()
        self.tick()

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
                    client.catalog = catalog if catalog is not None else client.devices()
                    device = next((item for item in client.catalog if item.serial == serial), None)
                    client.profile = client.profile_catalog.resolve(dict(settings.device_profile_assignments).get(serial, ""), device.model_id if device else "", device.name if device else "")
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
        self.changed.emit()

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
            return self.credential_cache.get(identity, settings.onepassword_cache_ttl_seconds, cancelled, load)
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
        if not profile_changed and credentials is None and replace(previous, logging_mode=settings.logging_mode, polling=settings.polling) == settings:
            self._update_demand()
            if previous.polling != settings.polling and self.connection not in {ConnectionState.AUTH_ERROR, ConnectionState.API_ERROR, ConnectionState.NETWORK_UNAVAILABLE, ConnectionState.DEVICE_OFFLINE, ConnectionState.UNSUPPORTED}:
                self._next_api = self.polling.next_due(self.clock())
            self.preferences_changed.emit()
            self.changed.emit()
            return
        self._credential_cancelled.set()
        self._credential_cancelled = threading.Event()
        self.credential_renewal_pending = False
        i18n.setup(settings.language)
        if hasattr(self.integration_adapter, "retain_sessions"):
            self.integration_adapter.retain_sessions(settings.integrations)
        self._generation += 1
        self._discovery_pending = None
        self._diagnostic_pending = False
        credential_fields = ("serial", "credential_ref", "credential_source", "onepassword_access_ref", "onepassword_secret_ref")
        if profile_changed or any(getattr(previous, name) != getattr(settings, name) for name in credential_fields):
            self.telemetry = None
            self.polling = PollingState(settings.polling)
            self.connection = ConnectionState.CONNECTING if settings.serial else ConnectionState.UNCONFIGURED
        if profile_changed:
            if settings.serial:
                self.metadata.sampler(settings.serial).previous = None
            for state in self.integration_states.values():
                state.gate.suspend()
        self._next_api = 0
        self._update_demand()
        self._api_pending = bool(settings.serial)
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
        self.integration_states = {
            module.id: self.integration_states[module.id] if previous_modules.get(module.id) == module else IntegrationState()
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
        if not state.busy:
            state.result = None
        self.update_preferences(integrations=tuple(replace(item, auto_enabled=enabled) if item.id == integration_id else item for item in self.settings.integrations))

    def request_profile(self, integration_id: str, profile_id: str, automatic: bool = False) -> None:
        if self._stopped or self.system_activity.paused:
            return
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
        state.transition_from = previous_id
        generation = self._generation
        activity_epoch = self._activity_epoch
        state.busy = True
        state.requested_profile = profile_id
        state.result = None
        def current():
            return (not self._stopped and not self.system_activity.paused and generation == self._generation
                    and activity_epoch == self._activity_epoch and self.module(integration_id).enabled)
        def operation(target_profile: IntegrationProfile = target):
            return profile_id, automatic, self.integration_adapter.transition(module, previous, target_profile, current)
        self._submit(kind, operation)
        if kind in self._active:
            self._active[kind].integration_state = state
        self.changed.emit()

    def tick(self) -> None:
        if self._stopped or self.system_activity.paused:
            return
        self._check_credential_expiry()
        now = self.clock()
        self._update_demand()
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
        if self.access_allowed and self.settings.serial and now >= self._next_api and "api" not in self._active:
            self.refresh(user_initiated=False)
        for module in self.settings.integrations:
            state = self.integration_states[module.id]
            kind = "scan:" + module.id
            if module.enabled and now >= state.next_scan and kind not in self._active:
                state.next_scan = now + 2
                self._submit(kind, lambda selected=module: self.integration_adapter.inspect(selected))
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
            if state.busy or (state.result is not None and not state.result.success):
                continue
            rule = module.select(self.telemetry, state.observation.running)
            profile_id = state.gate.evaluate(rule.profile_id if rule else None, now, state.active_profile)
            if profile_id is not None:
                self.request_profile(module.id, profile_id, automatic=True)

    def _completed(self, kind, generation, result, error) -> None:
        previous_connection = self.connection
        task = self._active.pop(kind, None)
        if kind.startswith("elevate:"):
            if hasattr(self.integration_adapter, "retain_sessions"):
                self.integration_adapter.retain_sessions(() if self._stopped else self.settings.integrations)
        if self._stopped:
            self.task_finished.emit(kind)
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
                    if len(result) > 3 and result[3] is not None:
                        self.profile_catalog = result[3]
                    if len(result) > 2 and result[2] is not None and self.catalog is None:
                        self.catalog = result[2]
                        self.metadata.remember(result[2])
                        self._save_metadata()
                    if isinstance(result[1], TelemetryPatch):
                        patch = result[1]
                        now, utc = self.clock(), time.time()
                        self.polling.complete(patch, utc, now)
                        if self._resume_fields is not None:
                            self._resume_fields.update(key for key, value in patch.values)
                        if self.metadata.sampler(self.settings.serial).observe(dict(patch.values), utc, now, self.settings.polling.primary.stale_after_seconds):
                            self._save_metadata()
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
                    if self.notice.startswith("onepassword_") or self.notice in {"credentials_required", "credentials_error", *(state.value for state in ConnectionState)}:
                        self.notice = ""
                if self.credential_renewal_pending:
                    self.credential_renewal_pending = False
                    self.credential_renewal_required = not self.credential_cache.ready
            elif kind.startswith("scan:"):
                integration_id = kind.split(":", 1)[1]
                previous_availability = self.integration_states[integration_id].observation.availability
                self.integration_states[integration_id].observation = result if error is None else IntegrationObservation("integration_error")
                if error is None:
                    self._resume_scans.discard(integration_id)
                availability = self.integration_states[integration_id].observation.availability
                if previous_availability != availability:
                    event("integration_availability_changed", module_index=self.settings.integrations.index(self.module(integration_id)) + 1, state=availability)
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
            state.transition_from = None
        elif not cancelled or outcome.completed:
            state.active_profile = None
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
        elif self.notice in {"profile_failed", "profile_unavailable"}:
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
        if hasattr(self.integration_adapter, "close"):
            self.integration_adapter.close()
        self._pool.waitForDone(22000)
        if self.metadata.devices or self.metadata.samplers:
            self.metadata.save()
        if hasattr(self.integration_adapter, "close"):
            self.integration_adapter.close()
        event("application_stopped")

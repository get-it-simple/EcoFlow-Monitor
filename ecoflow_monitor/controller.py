from dataclasses import replace
import logging
import time
import uuid
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.afterburner import AfterburnerAdapter
from ecoflow_monitor.adapters.credentials import CredentialError, CredentialStore
from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError
from ecoflow_monitor.adapters.hotkeys import validate_bindings
from ecoflow_monitor.adapters.windows import internet_unavailable
from ecoflow_monitor.domain import AfterburnerObservation, AutomationGate, Availability, ConnectionState, resolve_limit, runtime_hours, select_rule
from ecoflow_monitor.settings import AppSettings, SettingsError, SettingsStore


logger = logging.getLogger(__name__)


class TaskSignals(QObject):
    finished = Signal(str, int, object, object)


class Task(QRunnable):
    def __init__(self, kind: str, generation: int, operation: Callable) -> None:
        super().__init__()
        self.kind = kind
        self.generation = generation
        self.operation = operation
        self.signals = TaskSignals()

    def run(self) -> None:
        result = None
        failure = None
        try:
            result = self.operation()
        except Exception as error:
            failure = error
        self.signals.finished.emit(self.kind, self.generation, result, failure)


class Controller(QObject):
    changed = Signal()
    preferences_changed = Signal()
    devices_ready = Signal(object)

    def __init__(self, store: SettingsStore, credential_store=None, afterburner=None, client_factory=None, clock=time.monotonic) -> None:
        super().__init__()
        self.store = store
        self.credential_store = credential_store if credential_store is not None else CredentialStore()
        self.afterburner = afterburner if afterburner is not None else AfterburnerAdapter()
        self.client_factory = client_factory if client_factory is not None else EcoFlowClient
        self.clock = clock
        self.notice = ""
        try:
            self.settings = store.load()
        except SettingsError as error:
            self.settings = AppSettings()
            self.notice = str(error)
        i18n.setup(self.settings.language)
        self.connection = ConnectionState.UNCONFIGURED
        self.telemetry = None
        self.observation = AfterburnerObservation(Availability.DISABLED)
        self.running = frozenset()
        self.requested_slot = None
        self.auto_reason = "automation_paused"
        self._gate = AutomationGate()
        self._generation = 0
        self._stopped = False
        self._active = {}
        self._api_pending = False
        self._discovery_pending = None
        self._api_mode = "telemetry"
        self._dispatch_request = None
        self._next_api = 0.0
        self._next_msi = 0.0
        self._retry = 5.0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(3)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.tick)

    @property
    def fresh(self) -> bool:
        return self.connection == ConnectionState.LIVE and self.telemetry is not None and self.clock() - self.telemetry.received_monotonic < 90

    @property
    def minimum(self):
        return resolve_limit(self.telemetry.min_soc if self.telemetry else None, self.settings.manual_min)

    @property
    def maximum(self):
        return resolve_limit(self.telemetry.max_soc if self.telemetry else None, self.settings.manual_max)

    @property
    def runtime(self):
        return runtime_hours(self.telemetry, self.settings.capacity_wh, self.minimum.value, self.fresh)

    @property
    def profiles(self):
        discovered = {profile.slot: profile for profile in self.observation.profiles}
        return tuple(discovered.get(profile.slot, profile) for profile in self.settings.profiles)

    def start(self) -> None:
        self._timer.start()
        self.tick()

    def _submit(self, kind: str, operation: Callable) -> None:
        if self._stopped or kind in self._active:
            return
        task = Task(kind, self._generation, operation)
        task.signals.finished.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._active[kind] = task
        self._pool.start(task)

    def discover(self, supplied: Credentials | None = None) -> None:
        if "api" in self._active:
            self._discovery_pending = (supplied,)
            return
        reference = self.settings.credential_ref
        self._api_mode = "devices"
        def operation():
            credentials = supplied if supplied is not None else self.credential_store.read(reference)
            if credentials is None:
                raise CredentialError("credentials_required")
            return ("devices", self.client_factory(credentials).devices())
        self._submit("api", operation)

    def refresh(self) -> None:
        if self._stopped:
            return
        if "api" in self._active:
            self._api_pending = True
            return
        if not self.settings.serial:
            self.connection = ConnectionState.UNCONFIGURED
            self.changed.emit()
            return
        self._api_pending = False
        serial, reference = self.settings.serial, self.settings.credential_ref
        self._api_mode = "telemetry"
        def operation():
            credentials = self.credential_store.read(reference)
            if credentials is None:
                raise CredentialError("credentials_required")
            try:
                return ("telemetry", self.client_factory(credentials).read(serial))
            except EcoFlowError as error:
                if error.state == ConnectionState.API_ERROR and internet_unavailable():
                    raise EcoFlowError(ConnectionState.NETWORK_UNAVAILABLE) from None
                raise
        if self.telemetry is None:
            self.connection = ConnectionState.CONNECTING
        self._next_api = self.clock() + 30
        self._submit("api", operation)
        self.changed.emit()

    def apply_settings(self, settings: AppSettings, credentials: Credentials | None = None) -> None:
        settings.validate()
        validate_bindings(settings.hotkeys)
        if credentials is not None:
            settings = replace(settings, credential_ref="EcoFlowMonitor/" + uuid.uuid4().hex)
            self.credential_store.write(settings.credential_ref, credentials)
        try:
            self.store.save(settings, explicit=True)
        except SettingsError:
            if credentials is not None and hasattr(self.credential_store, "delete"):
                try:
                    self.credential_store.delete(settings.credential_ref)
                except CredentialError:
                    logger.warning("An unused credential target could not be discarded")
            raise
        previous = self.settings
        self.settings = settings
        i18n.setup(settings.language)
        self._generation += 1
        self._discovery_pending = None
        self._gate.reset()
        if (previous.serial, previous.credential_ref) != (settings.serial, settings.credential_ref):
            self.telemetry = None
            self.connection = ConnectionState.CONNECTING if settings.serial else ConnectionState.UNCONFIGURED
        self._next_api = 0
        self._next_msi = 0
        self._api_pending = bool(settings.serial)
        self._retry = 5
        self.requested_slot = None
        self.notice = ""
        if not settings.msi_enabled:
            self.observation = AfterburnerObservation(Availability.DISABLED)
            self.running = frozenset()
        self.preferences_changed.emit()
        self.changed.emit()

    def update_preferences(self, **changes) -> None:
        settings = replace(self.settings, **changes)
        try:
            self.store.save(settings)
        except SettingsError as error:
            self.notice = str(error)
        self.settings = settings
        if set(changes) - {"monitor_geometry", "mini_position", "auto_enabled", "monitor_visible"}:
            self.preferences_changed.emit()
        self.changed.emit()

    def set_auto(self, enabled: bool) -> None:
        if not self.settings.msi_enabled:
            return
        self._gate.reset()
        self.update_preferences(auto_enabled=enabled)

    def request_profile(self, slot: int, automatic: bool = False) -> None:
        if not automatic:
            self.set_auto(False)
        if not self.settings.msi_enabled or self.observation.availability != Availability.READY or self.observation.detail or "dispatch" in self._active:
            self.notice = "profile_unavailable"
            self.changed.emit()
            return
        settings = self.settings
        generation = self._generation
        self._dispatch_request = (slot, automatic)
        def operation():
            if self._stopped or generation != self._generation or not self.settings.msi_enabled:
                return (slot, False, automatic)
            return (slot, self.afterburner.dispatch(settings.msi_executable, slot, settings.gpu_id), automatic)
        self._submit("dispatch", operation)
        self.changed.emit()

    def tick(self) -> None:
        if self._stopped:
            return
        now = self.clock()
        if self.connection == ConnectionState.LIVE and not self.fresh:
            self.connection = ConnectionState.STALE
            self.changed.emit()
        if self.settings.serial and now >= self._next_api and "api" not in self._active:
            self.refresh()
        if self.settings.msi_enabled and now >= self._next_msi and "msi" not in self._active:
            settings = self.settings
            self._next_msi = now + 2
            self._submit("msi", lambda: self.afterburner.inspect(settings.msi_executable, settings.gpu_id))
        eligible = self.settings.msi_enabled and self.settings.auto_enabled and self.fresh and self.observation.availability == Availability.READY and not self.observation.detail
        reason = "automatic_mode" if eligible else "automation_paused"
        if reason != self.auto_reason:
            self.auto_reason = reason
            self.changed.emit()
        if not eligible:
            self._gate.suspend()
            return
        rule = select_rule(self.telemetry, self.running, self.settings.application_rules, self.settings.system_rules)
        slot = self._gate.evaluate(rule.slot if rule else None, now, self.observation.observed_slot)
        if slot is not None and "dispatch" not in self._active:
            self.request_profile(slot, automatic=True)

    def _completed(self, kind, generation, result, error) -> None:
        self._active.pop(kind, None)
        if self._stopped:
            return
        if generation == self._generation:
            if kind == "api":
                if error is not None:
                    state = error.state if isinstance(error, EcoFlowError) else ConnectionState.AUTH_ERROR if isinstance(error, CredentialError) else ConnectionState.API_ERROR
                    if isinstance(error, CredentialError):
                        self.notice = str(error)
                    elif self._api_mode == "devices":
                        self.notice = state.value
                    if self._api_mode != "devices":
                        self.connection = state
                        self._next_api = float("inf") if self.connection == ConnectionState.AUTH_ERROR else self.clock() + max(self._retry, getattr(error, "retry_after", None) or 0)
                        self._retry = min(self._retry * 2, 300)
                    self.devices_ready.emit(())
                elif result[0] == "devices":
                    self.devices_ready.emit(result[1])
                else:
                    self.telemetry = result[1]
                    self.connection = ConnectionState.LIVE
                    self._next_api = self.clock() + 30
                    self._retry = 5
                    if self.notice in {"credentials_required", "credentials_error", *(state.value for state in ConnectionState)}:
                        self.notice = ""
            elif kind == "msi":
                self.observation, self.running = result if error is None else (AfterburnerObservation(Availability.ERROR), frozenset())
            elif kind == "dispatch":
                slot, automatic = self._dispatch_request
                success = bool(result[1]) if error is None else False
                if automatic:
                    self._gate.dispatched(slot, self.clock(), success)
                self.requested_slot = slot if success else None
                if not success:
                    self.notice = "profile_failed"
                elif self.notice in {"profile_failed", "profile_unavailable"}:
                    self.notice = ""
                self._next_msi = 0
            if error is not None and not isinstance(error, (EcoFlowError, CredentialError)):
                logger.error("Background %s failed (%s)", kind, type(error).__name__)
            self.changed.emit()
        if kind == "dispatch":
            self._dispatch_request = None
        if kind == "api":
            if self._discovery_pending is not None:
                supplied, = self._discovery_pending
                self._discovery_pending = None
                self.discover(supplied)
            elif self._api_pending:
                self._api_pending = False
                self.refresh()

    def stop(self) -> None:
        self._stopped = True
        self._generation += 1
        self._timer.stop()
        self._pool.clear()
        self._pool.waitForDone(22000)

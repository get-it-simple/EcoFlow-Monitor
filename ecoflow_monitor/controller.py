from dataclasses import dataclass, field, replace
import logging
import threading
import time
import uuid
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation, TransitionResult
from ecoflow_monitor.adapters.credentials import CredentialError, CredentialStore
from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError
from ecoflow_monitor.adapters.onepassword import read_credentials
from ecoflow_monitor.adapters.hotkeys import validate_bindings
from ecoflow_monitor.adapters.windows import internet_unavailable
from ecoflow_monitor.domain import AutomationGate, ConnectionState, resolve_limit, runtime_hours
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile
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


class Controller(QObject):
    changed = Signal()
    preferences_changed = Signal()
    runtime_preferences_changed = Signal(object)
    devices_ready = Signal(object)
    elevation_needed = Signal(str)

    def __init__(self, store: SettingsStore, credential_store=None, integration_adapter=None, client_factory=None, clock=time.monotonic) -> None:
        super().__init__()
        self.store = store
        self.credential_store = credential_store if credential_store is not None else CredentialStore()
        self.integration_adapter = integration_adapter if integration_adapter is not None else IntegrationAdapter()
        self.client_factory = client_factory if client_factory is not None else EcoFlowClient
        self.clock = clock
        self.notice = ""
        self.preview = None
        try:
            self.settings = store.load()
        except SettingsError as error:
            self.settings = AppSettings()
            self.notice = str(error)
        i18n.setup(self.settings.language)
        self.connection = ConnectionState.UNCONFIGURED
        self.telemetry = None
        self.integration_states = {module.id: IntegrationState() for module in self.settings.integrations}
        self._generation = 0
        self._stopped = False
        self._credential_cancelled = threading.Event()
        self._active = {}
        self._api_pending = False
        self._discovery_pending = None
        self._api_mode = "telemetry"
        self._next_api = 0.0
        self._retry = 5.0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(3)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.tick)

    @property
    def ui_settings(self):
        return self.preview if self.preview is not None else self.settings

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

    def module(self, integration_id: str) -> IntegrationDefinition:
        return next(module for module in self.settings.integrations if module.id == integration_id)

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
        settings, cancelled = self.settings, self._credential_cancelled
        self._api_mode = "devices"
        def operation():
            credentials = supplied if supplied is not None else self._read_credentials(settings, cancelled)
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
        settings, cancelled = self.settings, self._credential_cancelled
        serial = settings.serial
        self._api_mode = "telemetry"
        def operation():
            credentials = self._read_credentials(settings, cancelled)
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

    def _read_credentials(self, settings: AppSettings, cancelled: threading.Event) -> Credentials | None:
        if settings.credential_source == "1password":
            return read_credentials(settings.onepassword_access_ref, settings.onepassword_secret_ref, cancelled)
        return self.credential_store.read(settings.credential_ref)

    def apply_settings(self, settings: AppSettings, credentials: Credentials | None = None) -> None:
        settings.validate()
        validate_bindings(settings.hotkeys)
        write_local = credentials is not None and settings.credential_source == "windows"
        if write_local:
            settings = replace(settings, credential_ref="EcoFlowMonitor/" + uuid.uuid4().hex)
            self.credential_store.write(settings.credential_ref, credentials)
        try:
            self.store.save(settings, explicit=True)
        except SettingsError:
            if write_local and hasattr(self.credential_store, "delete"):
                try:
                    self.credential_store.delete(settings.credential_ref)
                except CredentialError:
                    logger.warning("An unused credential target could not be discarded")
            raise
        previous = self.settings
        self.settings = settings
        self._credential_cancelled.set()
        self._credential_cancelled = threading.Event()
        i18n.setup(settings.language)
        if hasattr(self.integration_adapter, "retain_sessions"):
            self.integration_adapter.retain_sessions(settings.integrations)
        self._generation += 1
        self._discovery_pending = None
        credential_fields = ("serial", "credential_ref", "credential_source", "onepassword_access_ref", "onepassword_secret_ref")
        if any(getattr(previous, name) != getattr(settings, name) for name in credential_fields):
            self.telemetry = None
            self.connection = ConnectionState.CONNECTING if settings.serial else ConnectionState.UNCONFIGURED
        self._next_api = 0
        self._api_pending = bool(settings.serial)
        self._retry = 5
        self.notice = ""
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

    def update_preferences(self, *, mini_overlay_fields: tuple[str, ...] = (), **changes) -> None:
        previous = self.settings
        settings = replace(previous, **changes)
        overlay_edits = {}
        if "mini_overlay" in changes:
            overlay_edits = {name: getattr(settings.mini_overlay, name) for name in settings.mini_overlay.__dataclass_fields__ if name in mini_overlay_fields or getattr(previous.mini_overlay, name) != getattr(settings.mini_overlay, name)}
        try:
            self.store.save(settings)
        except SettingsError as error:
            self.notice = str(error)
        self.settings = settings
        if "integrations" in changes and hasattr(self.integration_adapter, "retain_sessions"):
            self.integration_adapter.retain_sessions(settings.integrations)
        if self.preview is not None:
            immediate = {name: value for name, value in changes.items() if name in {"mini_enabled", "mini_position"}}
            if overlay_edits:
                immediate["mini_overlay"] = replace(self.preview.mini_overlay, **overlay_edits)
            self.preview = replace(self.preview, **immediate)
        self.runtime_preferences_changed.emit({**changes, "mini_overlay_fields": tuple(overlay_edits)})
        if set(changes) - {"monitor_geometry", "mini_position", "monitor_visible"}:
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
        module = self.module(integration_id)
        if not automatic:
            self.set_auto(integration_id, False)
            module = self.module(integration_id)
        state = self.integration_states[integration_id]
        kind = "dispatch:" + integration_id
        target = next((profile for profile in module.profiles if profile.id == profile_id), None)
        if not module.enabled or not state.observation.ready or kind in self._active or target is None:
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
        state.busy = True
        state.requested_profile = profile_id
        state.result = None
        def current():
            return not self._stopped and generation == self._generation and self.module(integration_id).enabled
        def operation(target_profile: IntegrationProfile = target):
            return profile_id, automatic, self.integration_adapter.transition(module, previous, target_profile, current)
        self._submit(kind, operation)
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
        for module in self.settings.integrations:
            state = self.integration_states[module.id]
            kind = "scan:" + module.id
            if module.enabled and now >= state.next_scan and kind not in self._active:
                state.next_scan = now + 2
                self._submit(kind, lambda selected=module: self.integration_adapter.inspect(selected))
            eligible = module.enabled and module.auto_enabled and self.fresh and state.observation.ready
            reason = "automatic_mode" if eligible else "automation_paused"
            if reason != state.auto_reason:
                state.auto_reason = reason
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
        self._active.pop(kind, None)
        if kind.startswith("elevate:"):
            if hasattr(self.integration_adapter, "retain_sessions"):
                self.integration_adapter.retain_sessions(() if self._stopped else self.settings.integrations)
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
                    if self.notice.startswith("onepassword_") or self.notice in {"credentials_required", "credentials_error", *(state.value for state in ConnectionState)}:
                        self.notice = ""
            elif kind.startswith("scan:"):
                integration_id = kind.split(":", 1)[1]
                self.integration_states[integration_id].observation = result if error is None else IntegrationObservation("integration_error")
            elif kind.startswith("elevate:"):
                self.notice = "integration_helper_failed" if error else "integration_helper_ready"
            elif kind.startswith("elevation_check:"):
                if result is True and error is None:
                    self.elevation_needed.emit(kind.split(":", 1)[1])
            elif kind.startswith("dispatch:"):
                integration_id = kind.split(":", 1)[1]
                state = self.integration_states[integration_id]
                profile_id, automatic, outcome = result if error is None else (state.requested_profile, True, TransitionResult(False, error="integration_command_failed"))
                state.busy = False
                state.result = outcome
                state.active_profile = profile_id if outcome.success else None
                if outcome.success:
                    state.transition_from = None
                if automatic:
                    state.gate.dispatched(profile_id, self.clock(), outcome.success)
                if not outcome.success:
                    self.notice = "profile_failed"
                    if outcome.winerror in (5, 740):
                        self.elevation_needed.emit(integration_id)
                elif self.notice in {"profile_failed", "profile_unavailable"}:
                    self.notice = ""
                state.next_scan = 0
            if error is not None and not isinstance(error, (EcoFlowError, CredentialError)):
                logger.error("Background %s failed (%s)", kind, type(error).__name__)
            self.changed.emit()
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
        self._credential_cancelled.set()
        self._generation += 1
        self._timer.stop()
        self._pool.clear()
        if hasattr(self.integration_adapter, "close"):
            self.integration_adapter.close()
        self._pool.waitForDone(22000)
        if hasattr(self.integration_adapter, "close"):
            self.integration_adapter.close()

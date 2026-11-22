import ctypes
from ctypes import wintypes
from dataclasses import replace
from itertools import permutations
import subprocess
import sys
import threading

import httpx
import pytest
from PySide6.QtWidgets import QMessageBox, QWidget
from qt_helpers import close_runtime, wait_until

from ecoflow_monitor.adapters.ecoflow import Credentials, Device, EcoFlowClient
from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation, TransitionResult
from ecoflow_monitor.adapters.onepassword import RequestCancelled
from ecoflow_monitor.adapters.system_activity import PowerSetting, SESSION_DISPLAY, SystemActivityWatcher
from ecoflow_monitor.adapters.system_activity import SessionInfo, WindowsActivity
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller, Task
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.integrations import IntegrationRule, Reaction
from ecoflow_monitor.polling import CORE, TelemetryPatch
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.system_activity import ActivityCancelled, SystemActivity
from ecoflow_monitor.ui.confirmations import ConfirmationCoordinator, ConfirmationRequest
from ecoflow_monitor.ui.dashboard import Dashboard


@pytest.mark.parametrize("pause,resume", [("suspend", "resume_user"), ("lock", "unlock"), ("display_off", "display_on")])
def test_independent_reasons_pause_and_resume(pause, resume):
    initial = SystemActivity()
    paused = initial.transition(pause)
    assert paused.paused
    assert paused.transition(pause) == paused
    assert paused.transition(resume) == initial


@pytest.mark.parametrize("order", list(permutations(("resume_user", "unlock", "display_on"))))
def test_overlapping_reasons_require_every_release(order):
    state = SystemActivity().transition("lock").transition("display_off").transition("suspend")
    for index, name in enumerate(order):
        state = state.transition(name)
        assert state.paused == (index < 2)


def test_automatic_wake_waits_for_user_and_dim_is_visible():
    state = SystemActivity().transition("suspend").transition("resume")
    assert state.paused and not state.sleeping
    assert state.transition("resume") == state
    assert not state.transition("display_dim").paused
    assert SystemActivity(locked=True).transition("display_dim").paused
    assert not SystemActivity(display_off=True).transition("display_dim").paused


def test_queued_task_does_not_start_after_cancellation(qapp):
    cancelled = threading.Event()
    calls, results = [], []
    task = Task("synthetic", 1, lambda: calls.append(True), cancelled)
    task.signals.finished.connect(lambda *args: results.append(args))
    cancelled.set()
    task.run()
    assert not calls and isinstance(results[0][3], ActivityCancelled)


def test_helper_does_not_launch_if_pause_arrives_during_target_check(activity_controller, monkeypatch):
    controller, now, calls = activity_controller
    adapter = IntegrationAdapter()
    def inspect(module):
        controller.set_system_activity(SystemActivity(locked=True))
        return IntegrationObservation("integration_ready", executable=sys.executable)
    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.launch_helper", lambda *args: pytest.fail("Unexpected UAC launch"))
    with pytest.raises(ActivityCancelled):
        adapter.start_elevated(controller.settings.integrations[0], lambda: not controller.system_activity.paused)
    assert not adapter.sessions


def test_helper_checks_cancellation_after_opening_listener(activity_controller, monkeypatch):
    from ecoflow_monitor.adapters.elevation import ElevatedSession
    controller, now, calls = activity_controller
    class Listener:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            controller.set_system_activity(SystemActivity(locked=True))
            return self

        def __exit__(self, *args):
            pass
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.Listener", Listener)
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.launch_helper", lambda *args: pytest.fail("Unexpected UAC launch"))
    session = ElevatedSession(controller.settings.integrations[0], sys.executable)
    with pytest.raises(ActivityCancelled):
        session.start(lambda: not controller.system_activity.paused)
    assert session.handle is None and session.connection is None


class ActivityBackend:
    def __init__(self, locked=False, failure=""):
        self.is_locked = locked
        self.failure = failure
        self.calls = []

    def _call(self, name, value=None):
        self.calls.append((name, value))
        if name == self.failure:
            raise OSError("Synthetic registration failure")

    def locked(self):
        self._call("locked")
        return self.is_locked

    def register_session(self, hwnd):
        self._call("register_session", hwnd)

    def register_power(self, hwnd):
        self._call("register_power", hwnd)
        return 0x1234567887654321

    def unregister_session(self, hwnd):
        self._call("unregister_session", hwnd)

    def unregister_power(self, handle):
        self._call("unregister_power", handle)


@pytest.fixture
def watcher(qapp):
    backend = ActivityBackend(locked=True)
    instance = SystemActivityWatcher(backend=backend)
    instance.start()
    yield instance, backend
    instance.stop()
    instance.deleteLater()
    qapp.processEvents()


def deliver(watcher, message, value, data=0, hwnd=None):
    msg = wintypes.MSG()
    msg.hWnd = watcher._hwnd if hwnd is None else hwnd
    msg.message, msg.wParam, msg.lParam = message, value, data
    return watcher.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows message layout")
def test_native_messages_current_session_and_hidden_window_lifecycle(watcher):
    instance, backend = watcher
    states = []
    instance.changed.connect(states.append)
    assert instance.state.locked and not instance.window.isVisible()
    backend.is_locked = False
    deliver(instance, 0x2B1, 8)
    assert not instance.state.paused
    deliver(instance, 0x2B1, 7, hwnd=instance._hwnd + 1)
    assert not instance.state.paused
    assert deliver(instance, 0x218, 4) == (True, 1)
    assert deliver(instance, 0x218, 4) == (True, 1)
    assert len(states) == 2
    deliver(instance, 0x218, 18)
    assert instance.state.paused
    deliver(instance, 0x218, 7)
    assert not instance.state.paused
    deliver(instance, 0x2B1, 4)
    assert instance.state.locked
    backend.is_locked = False
    deliver(instance, 0x2B1, 3)
    assert not instance.state.paused
    original = instance._hwnd
    instance.stop()
    instance.stop()
    assert backend.calls.count(("unregister_power", 0x1234567887654321)) == 1
    assert backend.calls.count(("unregister_session", original)) == 1
    assert instance.window is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows message layout")
def test_native_display_values_and_invalid_payloads(watcher):
    instance, backend = watcher
    deliver(instance, 0x2B1, 8)
    setting = PowerSetting()
    setting.guid[:] = SESSION_DISPLAY
    setting.length, setting.value = 4, 0
    assert deliver(instance, 0x218, 0x8013, ctypes.addressof(setting)) == (True, 1)
    assert instance.state.display_off
    for length, value in ((0, 1), (8, 1), (4, 3)):
        setting.length, setting.value = length, value
        deliver(instance, 0x218, 0x8013, ctypes.addressof(setting))
        assert instance.state.display_off
    setting.length, setting.value = 4, 2
    deliver(instance, 0x218, 0x8013, ctypes.addressof(setting))
    assert not instance.state.paused
    setting.guid[0] ^= 1
    setting.value = 0
    deliver(instance, 0x218, 0x8013, ctypes.addressof(setting))
    assert not instance.state.paused
    assert deliver(instance, 0x218, 0x8013, 0) == (False, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows message layout")
def test_wake_rechecks_lock_without_treating_query_as_user_presence(watcher):
    instance, backend = watcher
    backend.is_locked = False
    deliver(instance, 0x2B1, 8)
    deliver(instance, 0x218, 4)
    deliver(instance, 0x218, 18)
    assert instance.state.awaiting_user and instance.state.paused
    backend.is_locked = True
    deliver(instance, 0x218, 7)
    assert instance.state.locked and instance.state.paused
    deliver(instance, 0x2B1, 8)
    assert not instance.state.paused


class NativeCall:
    def __init__(self, operation):
        self.operation = operation

    def __call__(self, *args):
        return self.operation(*args)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes layouts")
@pytest.mark.parametrize("state,flags,locked", [(0, 0, True), (0, 1, False), (4, 1, True)])
def test_windows_query_parses_aligned_header_and_frees_buffer(monkeypatch, state, flags, locked):
    info = SessionInfo()
    info.level, info.data.info.state, info.data.info.flags = 1, state, flags
    freed = []
    class Library:
        pass
    user, wts = Library(), Library()
    user.RegisterPowerSettingNotification = NativeCall(lambda *args: 0x1234567887654321)
    user.UnregisterPowerSettingNotification = NativeCall(lambda *args: True)
    wts.WTSRegisterSessionNotification = NativeCall(lambda *args: True)
    wts.WTSUnRegisterSessionNotification = NativeCall(lambda *args: True)
    def query(handle, session, kind, output, size):
        assert session == 0xFFFFFFFF and kind == 25
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(info)
        ctypes.cast(size, ctypes.POINTER(wintypes.DWORD))[0] = ctypes.sizeof(info)
        return True
    wts.WTSQuerySessionInformationW = NativeCall(query)
    wts.WTSFreeMemory = NativeCall(lambda address: freed.append(address.value))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: user if name == "user32" else wts)
    backend = WindowsActivity()
    assert SessionInfo.data.offset == 8
    assert backend.locked() == locked
    assert freed == [ctypes.addressof(info)]
    assert backend.register_power(123) == 0x1234567887654321
    info.level = 0
    with pytest.raises(OSError, match="Unknown session"):
        backend.locked()
    assert freed == [ctypes.addressof(info)] * 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows window adapter")
@pytest.mark.parametrize("failure", ["register_session", "register_power", "locked", "unregister_power", "unregister_session"])
def test_registration_failures_preserve_available_notifications_and_cleanup(qapp, failure):
    backend = ActivityBackend(failure=failure)
    instance = SystemActivityWatcher(backend=backend)
    try:
        instance.start()
        assert instance.window is not None and not instance.window.isVisible()
        deliver(instance, 0x218, 4)
        assert instance.state.paused
    finally:
        instance.stop()
        instance.deleteLater()
        qapp.processEvents()


class CredentialsStore:
    def __init__(self):
        self.calls = 0

    def read(self, reference):
        self.calls += 1
        return Credentials("synthetic", "synthetic")


class Integrations:
    def __init__(self):
        self.scans = 0
        self.commands = []

    def inspect(self, module):
        self.scans += 1
        return IntegrationObservation("integration_ready")

    def transition(self, module, previous, target, current):
        assert current()
        self.commands.append(target.id)
        return TransitionResult(True, 1)


@pytest.fixture
def activity_controller(tmp_path, qapp, session_cache):
    now, calls = [1000.0], []
    class Client:
        def read_quotas(self, serial, select, **kwargs):
            quotas = select()
            calls.append(quotas)
            keys = quotas
            values = {"input_power": 0, "output_power": 100, "battery_level": 75,
                      "minimum_discharge_level": 10, "maximum_charge_level": 90,
                      "ac_enabled": True, "dc_enabled": False, "charging_state": 1, "anderson": ()}
            return TelemetryPatch(keys, tuple((key, values.get(key, 1)) for key in keys))

        def devices(self):
            return (Device("TEST", "Synthetic", True),)

    instance = Controller(SettingsStore(tmp_path / "settings.json"), CredentialsStore(), Integrations(),
                          lambda credentials: Client(), lambda: now[0], session_cache)
    module = replace(instance.settings.integrations[0], enabled=True, auto_enabled=True,
                     system_rules=(IntegrationRule("fallback", "profile_5"),))
    instance.settings = replace(instance.settings, serial="TEST", integrations=(module,))
    instance._update_demand()
    yield instance, now, calls
    instance.stop()
    instance.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("reason", [SystemActivity(sleeping=True), SystemActivity(locked=True), SystemActivity(display_off=True)])
def test_pause_blocks_all_new_work_without_changing_auto_or_cache(activity_controller, qapp, reason):
    controller, now, calls = activity_controller
    controller.refresh()
    wait_until(qapp, lambda: not controller._active)
    snapshot, samples = controller.telemetry, dict(controller.polling.samples)
    settings, reads = controller.settings, controller.credential_store.calls
    controller.access_allowed = False
    controller.set_system_activity(reason)
    controller.refresh()
    controller.discover()
    controller.collect_diagnostics()
    controller.request_profile("afterburner", "profile_3")
    controller.tick()
    qapp.processEvents()
    assert not controller._timer.isActive() and not controller._active
    assert len(calls) == 1 and controller.credential_store.calls == reads
    assert not controller.integration_adapter.commands and not controller.integration_adapter.scans
    assert controller.settings is settings and controller.telemetry is snapshot
    assert controller.polling.samples == samples
    assert not controller.access_allowed


def test_initial_paused_controller_start_does_not_schedule_work(activity_controller, qapp):
    controller, now, calls = activity_controller
    controller.set_system_activity(SystemActivity(locked=True))
    controller.start(authorized=False)
    qapp.processEvents()
    assert not controller._timer.isActive() and not controller._active and not calls
    controller.set_system_activity(SystemActivity())
    qapp.processEvents()
    wait_until(qapp, lambda: not controller._active)
    assert controller._timer.isActive() and not controller.access_allowed and not calls


def test_resume_requires_valid_new_dependency_values(activity_controller, qapp):
    controller, now, calls = activity_controller
    controller.refresh()
    wait_until(qapp, lambda: not controller._active)
    received = controller.polling.samples["battery_level"].received_monotonic
    class MissingBattery:
        def read_quotas(self, serial, select, **kwargs):
            quotas = select()
            keys = quotas
            return TelemetryPatch(keys, (), missing=keys)
    controller.client_factory = lambda pair: MissingBattery()
    controller.set_system_activity(SystemActivity(locked=True))
    controller.set_system_activity(SystemActivity())
    qapp.processEvents()
    wait_until(qapp, lambda: not controller._active)
    assert not controller.fresh_for({"battery_level"})
    assert controller.polling.samples["battery_level"].received_monotonic == received
    now[0] += 10
    controller.tick()
    assert not controller.integration_adapter.commands


def test_scan_from_before_pause_cannot_unlock_automation(activity_controller, qapp):
    controller, now, calls = activity_controller
    started, release = threading.Event(), threading.Event()
    adapter = controller.integration_adapter
    original = adapter.inspect
    def inspect(module):
        started.set()
        assert release.wait(4)
        return original(module)
    adapter.inspect = inspect
    try:
        controller.tick()
        wait_until(qapp, started.is_set)
        controller.set_system_activity(SystemActivity(locked=True))
        controller.set_system_activity(SystemActivity())
        qapp.processEvents()
        release.set()
        wait_until(qapp, lambda: not controller._active)
        assert controller._resume_scans == {"afterburner"}
        assert not controller.integration_states["afterburner"].observation.ready
        controller.tick()
        wait_until(qapp, lambda: not controller._active)
        assert not controller._resume_scans
    finally:
        release.set()


def test_resume_refetches_even_fresh_cache_before_restarting_stability(activity_controller, qapp):
    controller, now, calls = activity_controller
    controller.refresh()
    wait_until(qapp, lambda: not controller._active)
    controller.tick()
    wait_until(qapp, lambda: not controller._active)
    now[0] += 4
    controller.set_system_activity(SystemActivity(locked=True))
    controller.set_system_activity(SystemActivity())
    assert not controller.fresh
    qapp.processEvents()
    wait_until(qapp, lambda: len(calls) == 2 and not controller._active)
    assert controller.fresh and not controller.integration_adapter.commands
    controller.tick()
    now[0] += 4.9
    controller.tick()
    assert not controller.integration_adapter.commands
    now[0] += 0.1
    controller.tick()
    wait_until(qapp, lambda: not controller._active)
    assert controller.integration_adapter.commands == ["profile_5"]
    assert controller.settings.integrations[0].auto_enabled


def test_inflight_http_result_is_discarded_and_only_one_refresh_follows(activity_controller, qapp):
    controller, now, calls = activity_controller
    started, release = threading.Event(), threading.Event()
    original = controller.client_factory
    class SlowClient:
        def read_quotas(self, serial, select, **kwargs):
            quotas = select()
            started.set()
            assert release.wait(4)
            return TelemetryPatch(tuple(CORE), (("battery_level", 1),))
    controller.client_factory = lambda pair: SlowClient()
    try:
        controller.refresh()
        wait_until(qapp, started.is_set)
        controller.set_system_activity(SystemActivity(sleeping=True, awaiting_user=True))
        controller.client_factory = original
        controller.set_system_activity(SystemActivity())
        qapp.processEvents()
        assert controller.telemetry is None and "api" in controller._active
        release.set()
        wait_until(qapp, lambda: len(calls) == 1 and not controller._active)
        assert controller.telemetry.soc_pct == 75
        assert not controller.polling.in_flight and controller.fresh
    finally:
        release.set()


def test_pause_between_http_calls_prevents_the_next_request(activity_controller, qapp):
    controller, now, calls = activity_controller
    started, release = threading.Event(), threading.Event()
    requests = []
    def handle(request):
        requests.append(request.method)
        started.set()
        assert release.wait(4)
        return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "online": 1}]})
    controller.client_factory = lambda pair: EcoFlowClient(pair, httpx.MockTransport(handle))
    try:
        controller.refresh()
        wait_until(qapp, started.is_set)
        controller.set_system_activity(SystemActivity(locked=True))
        release.set()
        wait_until(qapp, lambda: not controller._active)
        assert requests == ["GET"] and controller.telemetry is None
        assert not controller.polling.in_flight
    finally:
        release.set()


@pytest.mark.parametrize("commands,successful", [(2, False), (1, True)])
def test_running_cli_finishes_without_starting_next_step(activity_controller, qapp, monkeypatch, commands, successful):
    controller, now, calls = activity_controller
    started, release = threading.Event(), threading.Event()
    executed = []
    adapter = IntegrationAdapter()
    monkeypatch.setattr(adapter, "inspect", lambda module: IntegrationObservation("integration_ready", executable=sys.executable))
    def run(args, **kwargs):
        executed.append(args)
        started.set()
        assert release.wait(4)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    controller.integration_adapter = adapter
    module = controller.settings.integrations[0]
    profile = replace(module.profiles[4], activation=Reaction(True, ("{app} -profile5",) * commands))
    controller.settings = replace(controller.settings, integrations=(replace(module, profiles=(*module.profiles[:4], profile)),))
    state = controller.integration_states[module.id]
    state.observation = IntegrationObservation("integration_ready")
    try:
        controller.request_profile(module.id, profile.id, automatic=True)
        wait_until(qapp, started.is_set)
        controller.set_system_activity(SystemActivity(locked=True))
        controller.set_system_activity(SystemActivity())
        release.set()
        wait_until(qapp, lambda: "dispatch:" + module.id not in controller._active)
        assert len(executed) == 1 and not state.busy
        assert state.active_profile == (profile.id if successful else None)
        if successful:
            assert state.result is not None and state.result.success
        else:
            assert state.result is None
        assert state.transition_from is None and controller.settings.integrations[0].auto_enabled
    finally:
        release.set()


def test_credential_expiry_includes_paused_time_without_automatic_provider_read(activity_controller, qapp, monkeypatch):
    controller, now, calls = activity_controller
    identity = ("op://Synthetic/EcoFlow/access", "op://Synthetic/EcoFlow/secret")
    controller.settings = replace(controller.settings, credential_source="1password", onepassword_access_ref=identity[0],
                                  onepassword_secret_ref=identity[1], onepassword_cache_ttl_seconds=3600)
    utc = [1800000000.0]
    controller.credential_cache._clock = lambda: now[0]
    controller.credential_cache._utc_clock = lambda: utc[0]
    controller.credential_cache.adopt(identity, Credentials("synthetic", "synthetic"))
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", lambda *args: pytest.fail("Unexpected provider renewal"))
    controller.set_system_activity(SystemActivity(sleeping=True))
    utc[0] += 3601
    controller.set_system_activity(SystemActivity())
    qapp.processEvents()
    assert controller.credential_renewal_required and not controller.credential_cache.ready
    assert controller.connection == ConnectionState.AUTH_ERROR and not calls
    assert controller.credential_cache._storage.read() is None


def permission(owner):
    prompt = QMessageBox(owner)
    prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    return prompt


def test_confirmation_pause_preserves_questions_and_never_accepts_late_answer(qapp, monkeypatch):
    owner = QWidget()
    queue = ConfirmationCoordinator(owner)
    accepted, activated = [], []
    monkeypatch.setattr("ecoflow_monitor.ui.confirmations.activate_window", lambda window: activated.append(window))
    try:
        queue.enqueue(ConfirmationRequest("first", permission, lambda done: (accepted.append(True), done())))
        wait_until(qapp, lambda: queue.dialog is not None)
        queue.set_paused(True)
        queue.dialog.done(QMessageBox.StandardButton.Yes)
        queue.enqueue(ConfirmationRequest("second", permission, lambda done: done()))
        qapp.processEvents()
        assert not accepted and queue.dialog is None and len(queue.pending) == 2
        activated.clear()
        queue.set_paused(False)
        wait_until(qapp, lambda: queue.dialog is not None)
        assert queue.active.key == "first" and not activated
        queue.dialog.done(QMessageBox.StandardButton.Yes)
        wait_until(qapp, lambda: queue.active is not None and queue.active.key == "second")
        assert accepted == [True]
    finally:
        queue.close()
        owner.deleteLater()
        qapp.processEvents()


def test_provider_waiting_during_pause_is_cancellable(qapp):
    owner = QWidget()
    queue = ConfirmationCoordinator(owner)
    queue.set_paused(True)
    cancelled, entered = threading.Event(), threading.Event()
    results = []
    def work():
        try:
            queue.run_external(entered.set, cancelled)
        except RequestCancelled:
            results.append("cancelled")
    worker = threading.Thread(target=work)
    worker.start()
    try:
        wait_until(qapp, lambda: bool(queue.pending))
        assert not entered.is_set()
        cancelled.set()
        wait_until(qapp, lambda: not worker.is_alive())
        assert results == ["cancelled"]
        queue.set_paused(False)
        qapp.processEvents()
        assert not entered.is_set()
    finally:
        cancelled.set()
        queue.close()
        worker.join(2)
        owner.deleteLater()
        qapp.processEvents()


def test_runtime_pause_stops_motion_preserves_draft_and_does_not_activate_windows(activity_controller, qapp, monkeypatch):
    controller, now, calls = activity_controller
    activated = []
    class TrackedDashboard(Dashboard):
        def activateWindow(self):
            activated.append(True)
    monkeypatch.setattr("ecoflow_monitor.application.Dashboard", TrackedDashboard)
    runtime = Application(controller.store.path, native=False, controller=controller)
    try:
        runtime.dashboard.show()
        runtime.dashboard.onepassword.access_ref.setText("op://Unsaved/Station/access")
        controller.refresh()
        wait_until(qapp, lambda: not controller._active)
        visible = (runtime.dashboard.isVisible(), runtime.widget.isVisible())
        controller.set_system_activity(SystemActivity(display_off=True))
        assert not runtime.motion_driver._timer.isActive()
        assert runtime.confirmations._paused and runtime.dashboard.onepassword._paused
        controller.update_preferences(dashboard_geometry=(100, 100, 900, 700))
        assert runtime.dashboard.onepassword.access_ref.text() == "op://Unsaved/Station/access"
        controller.set_system_activity(SystemActivity())
        wait_until(qapp, lambda: len(calls) == 2 and not controller._active)
        assert runtime.dashboard.onepassword.access_ref.text() == "op://Unsaved/Station/access"
        assert (runtime.dashboard.isVisible(), runtime.widget.isVisible()) == visible
        assert not activated and not runtime.confirmations._paused
        assert not runtime.dashboard.onepassword._paused
    finally:
        close_runtime(runtime, qapp)


def test_provider_completion_is_delivered_only_after_resume(activity_controller, qapp):
    controller, now, calls = activity_controller
    runtime = Application(controller.store.path, native=False, controller=controller)
    release = threading.Event()
    panel = runtime.dashboard.onepassword
    credentials = Credentials("synthetic-access", "synthetic-secret")
    received = []
    panel.request_completed.connect(received.append)
    try:
        def load(cancelled):
            assert release.wait(4)
            return credentials

        panel._start(load, saving=False)
        controller.set_system_activity(SystemActivity(locked=True))
        release.set()
        wait_until(qapp, lambda: bool(received))
        assert panel.busy and panel.credentials is None
        controller.set_system_activity(SystemActivity())
        assert not panel.busy and panel.credentials is credentials
    finally:
        release.set()
        close_runtime(runtime, qapp)


def test_shutdown_cancels_a_pending_resume(activity_controller, qapp):
    controller, now, calls = activity_controller
    controller.set_system_activity(SystemActivity(locked=True))
    controller.set_system_activity(SystemActivity())
    controller.stop()
    qapp.processEvents()
    assert not calls and not controller._timer.isActive() and not controller._activity_timer.isActive()

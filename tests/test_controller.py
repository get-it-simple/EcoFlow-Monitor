from dataclasses import replace
import threading
import time

import pytest
from qt_helpers import wait_until as wait_for_condition

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowError
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.integrations import IntegrationRule
from ecoflow_monitor.adapters.integrations import IntegrationObservation, TransitionResult
from ecoflow_monitor.settings import SettingsStore


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def read(self, reference):
        return self.values.get(reference)

    def write(self, reference, credentials):
        self.values[reference] = credentials

    def delete(self, reference):
        self.values.pop(reference, None)


class FakeIntegrationAdapter:
    def __init__(self):
        self.inspections = 0
        self.commands = []

    def inspect(self, module):
        self.inspections += 1
        return IntegrationObservation("integration_ready")

    def transition(self, module, previous, target, current):
        self.commands.append(target.slot)
        return TransitionResult(True, 1)


def wait_until(qapp, condition):
    wait_for_condition(qapp, condition, timeout=4000)


@pytest.fixture
def controller(tmp_path, qapp):
    instance = Controller(SettingsStore(tmp_path / "settings.json"), MemoryCredentials(), FakeIntegrationAdapter())
    yield instance
    instance.stop()
    qapp.processEvents()


def test_disabled_integration_has_no_process_watcher(controller):
    controller.tick()
    assert controller.integration_adapter.inspections == 0
    assert controller.integration_states["afterburner"].observation.availability == "integration_disabled"


def test_manual_profile_turns_auto_off_before_external_command(controller, qapp):
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True, auto_enabled=True),)))
    controller.integration_states["afterburner"].observation = IntegrationObservation("integration_ready")
    controller.request_profile("afterburner", "profile_3")
    assert controller.module("afterburner").auto_enabled is False
    wait_until(qapp, lambda: "dispatch:afterburner" not in controller._active)
    assert controller.integration_adapter.commands == [3]
    assert controller.integration_states["afterburner"].requested_profile == "profile_3" and controller.integration_states["afterburner"].result.success


def test_offline_automation_cannot_dispatch(controller):
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True, auto_enabled=True, system_rules=(IntegrationRule("s", "profile_5"),)),)))
    controller.integration_states["afterburner"].observation = IntegrationObservation("integration_ready")
    controller.telemetry = Telemetry(0, 200, 80, 100, time.monotonic())
    controller.connection = ConnectionState.API_ERROR
    controller.tick()
    assert controller.integration_adapter.commands == []
    assert controller.runtime is None


def test_manual_retry_of_msi_slot_sends_again(controller, qapp):
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True),)))
    state = controller.integration_states["afterburner"]
    state.observation = IntegrationObservation("integration_ready")
    for _ in range(2):
        controller.request_profile("afterburner", "profile_5")
        wait_until(qapp, lambda: "dispatch:afterburner" not in controller._active)
    assert controller.integration_adapter.commands == [5, 5]
    assert state.active_profile == "profile_5"
    assert state.requested_profile == "profile_5" and state.result.success


def test_bad_credentials_discovery_does_not_replace_live_monitor_state(controller, qapp):
    class FailingClient:
        def devices(self):
            raise EcoFlowError(ConnectionState.AUTH_ERROR)
    controller.client_factory = lambda credentials: FailingClient()
    controller.telemetry = Telemetry(0, 200, 80, 100, time.monotonic())
    controller.connection = ConnectionState.LIVE
    controller.discover(Credentials("synthetic", "synthetic"))
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.connection == ConnectionState.LIVE and controller.fresh
    assert controller.notice == "authentication_error"


def test_device_change_rejects_old_job_result(controller, qapp):
    release = threading.Event()
    started = threading.Event()
    class SlowClient:
        def read(self, serial):
            started.set()
            release.wait(3)
            return Telemetry(0, 200, 80, 100, time.monotonic())
    controller.client_factory = lambda credentials: SlowClient()
    original = replace(controller.settings, serial="OLD")
    controller.apply_settings(original, Credentials("synthetic", "synthetic"))
    controller.refresh()
    try:
        wait_until(qapp, started.is_set)
        controller.apply_settings(replace(controller.settings, serial=""))
        release.set()
        wait_until(qapp, lambda: "api" not in controller._active)
        assert controller.telemetry is None and controller.connection == ConnectionState.UNCONFIGURED
    finally:
        release.set()


def test_refreshes_coalesce_to_one_pending_job(controller, qapp):
    release = threading.Event()
    calls = []
    class SlowClient:
        def read(self, serial):
            calls.append(serial)
            release.wait(3)
            return Telemetry(0, 200, 80, 100, time.monotonic())
    controller.client_factory = lambda credentials: SlowClient()
    controller.apply_settings(replace(controller.settings, serial="TEST"), Credentials("synthetic", "synthetic"))
    controller._api_pending = False
    controller.refresh()
    for _ in range(8):
        controller.refresh()
    try:
        release.set()
        wait_until(qapp, lambda: len(calls) == 2 and "api" not in controller._active)
        assert calls == ["TEST", "TEST"]
    finally:
        release.set()


def test_automatic_dispatch_stability_suspension_and_recovery(controller, qapp):
    now = [100.0]
    controller.clock = lambda: now[0]
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True, auto_enabled=True, system_rules=(IntegrationRule("system", "profile_5"),)),)))
    controller.integration_states["afterburner"].observation = IntegrationObservation("integration_ready")
    controller.telemetry = Telemetry(0, 200, 80, 100, now[0])
    controller.connection = ConnectionState.LIVE
    controller.tick()
    assert controller.integration_adapter.commands == []
    now[0] = 105
    controller.tick()
    wait_until(qapp, lambda: "dispatch:afterburner" not in controller._active)
    assert controller.integration_adapter.commands == [5]
    now[0] = 120
    controller.tick()
    assert controller.integration_adapter.commands == [5]
    now[0] = 195
    controller.tick()
    assert not controller.fresh and controller.connection == ConnectionState.STALE
    controller.telemetry = replace(controller.telemetry, received_monotonic=195)
    controller.connection = ConnectionState.LIVE
    controller.tick()
    now[0] = 200
    controller.tick()
    wait_until(qapp, lambda: "dispatch:afterburner" not in controller._active)
    assert controller.integration_adapter.commands == [5]


def test_unexpected_automatic_dispatch_error_does_not_loop(controller, qapp):
    now = [100.0]
    controller.clock = lambda: now[0]
    def fail(*args):
        raise RuntimeError("Synthetic external failure")
    controller.integration_adapter.transition = fail
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True, auto_enabled=True, system_rules=(IntegrationRule("system", "profile_5"),)),)))
    controller.integration_states["afterburner"].observation = IntegrationObservation("integration_ready")
    controller.telemetry = Telemetry(0, 200, 80, 100, now[0])
    controller.connection = ConnectionState.LIVE
    controller.tick()
    now[0] = 105
    controller.tick()
    wait_until(qapp, lambda: "dispatch:afterburner" not in controller._active)
    assert controller.notice == "profile_failed"
    now[0] = 120
    controller.tick()
    assert "dispatch:afterburner" not in controller._active


def test_failed_preferences_save_keeps_old_credentials_and_discards_new_target(controller, monkeypatch):
    from ecoflow_monitor.settings import SettingsError
    old = controller.settings.credential_ref
    controller.credential_store.write(old, Credentials("old-synthetic", "old-synthetic"))
    def fail(*args, **kwargs):
        raise SettingsError("settings_save_error")
    monkeypatch.setattr(controller.store, "save", fail)
    with pytest.raises(SettingsError):
        controller.apply_settings(controller.settings, Credentials("new-synthetic", "new-synthetic"))
    assert set(controller.credential_store.values) == {old}
    assert controller.credential_store.read(old).access_key == "old-synthetic"

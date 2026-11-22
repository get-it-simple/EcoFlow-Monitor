from dataclasses import replace
import queue
import threading
import time

import httpx
import pytest
from qt_helpers import wait_until as wait_for_condition

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError
from ecoflow_monitor.adapters.ecoflow_mqtt import MqttError, decode_report
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.integrations import IntegrationRule
from ecoflow_monitor.adapters.integrations import IntegrationObservation, TransitionResult
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.system_activity import SystemActivity


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


@pytest.fixture
def mqtt_monitor(controller, request):
    requests, streams = [], []
    certification_status = getattr(request, "param", 200)
    waiting_https = threading.Event()
    release_https = threading.Event()
    release_https.set()
    now = [1000.0]
    controller.clock = lambda: now[0]
    controller.settings = replace(controller.settings, serial="TEST")
    controller.credential_store.write(controller.settings.credential_ref, Credentials("synthetic-access", "synthetic-secret"))

    def handle(request):
        name = request.url.path.rsplit("/", 1)[-1]
        requests.append(name)
        if name == "list":
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "deviceName": "DELTA 3 MAX", "online": 1}]})
        if name == "certification":
            if certification_status != 200:
                return httpx.Response(certification_status)
            return httpx.Response(200, json={"code": "0", "data": {
                "url": "mqtt.ecoflow.com", "port": "8883", "protocol": "mqtts",
                "certificateAccount": "synthetic-account", "certificatePassword": "synthetic-password"}})
        assert name == "quota"
        waiting_https.set()
        assert release_https.wait(4)
        return httpx.Response(200, json={"code": "0", "data": {"powInSumW": 10, "powOutSumW": 20, "cmsBattSoc": 40}})

    def stream(access, serial, cancelled, receive):
        assert access.host == "mqtt.ecoflow.com" and serial == "TEST"
        reports = queue.Queue()
        streams.append((reports, cancelled, receive))
        while not cancelled.is_set():
            try:
                report = reports.get(timeout=0.02)
            except queue.Empty:
                continue
            if report is None:
                raise MqttError("stream_disconnected")
            receive(decode_report(report))

    controller.client_factory = lambda credentials: EcoFlowClient(credentials, httpx.MockTransport(handle))
    controller._mqtt_stream = stream
    yield now, requests, streams, waiting_https, release_https
    release_https.set()


def test_mqtt_push_stops_polling_and_recovers_after_https_fallback(controller, mqtt_monitor, qapp):
    now, requests, streams, waiting_https, release_https = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    assert controller.telemetry_source == "waiting_mqtt"
    reports, _, _ = streams[0]
    reports.put(b'{"cmsBattSoc":70,"powInSumW":0}')
    wait_until(qapp, lambda: "battery_level" in controller._mqtt_session.readings)
    assert controller.telemetry is None and requests == ["list", "certification"]
    reports.put(b'{"powOutSumW":50,"powGet12vList":{"powGet12vItem":[0,3]}}')
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    assert controller.fresh and controller.telemetry.soc_pct == 70 and controller.telemetry.input_w == 0
    assert dict(controller.telemetry.ports)["anderson_2"] == 3

    now[0] += 31
    reports.put(b'{"cmsBattSoc":71}')
    wait_until(qapp, lambda: controller.telemetry.soc_pct == 71)
    assert controller.polling.samples["input_power"].received_monotonic == now[0] - 31
    controller.tick()
    assert requests == ["list", "certification"]

    reports.put(None)
    wait_until(qapp, lambda: controller.telemetry_source == "https_fallback" and controller.telemetry.soc_pct == 40)
    assert requests.count("quota") == 1
    now[0] += 30
    waiting_https.clear()
    release_https.clear()
    controller.tick()
    wait_until(qapp, lambda: len(streams) == 2)
    controller.tick()
    wait_until(qapp, waiting_https.is_set)
    streams[1][0].put(b'{"cmsBattSoc":75,"powInSumW":0,"powOutSumW":65}')
    wait_until(qapp, lambda: controller.telemetry.soc_pct == 75)
    release_https.set()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.telemetry.soc_pct == 75 and controller.telemetry_source == "mqtt"
    assert requests.count("certification") == 2 and requests.count("quota") == 2


def test_mqtt_timeout_staleness_and_pause_reject_old_session_reports(controller, mqtt_monitor, qapp):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    now[0] += 20
    controller.tick()
    wait_until(qapp, lambda: controller.telemetry is not None)
    assert controller.telemetry_source == "https_fallback" and streams[0][1].is_set()

    now[0] += 30
    controller.tick()
    wait_until(qapp, lambda: len(streams) == 2)
    report = b'{"cmsBattSoc":80,"powInSumW":0,"powOutSumW":65}'
    streams[1][0].put(report)
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    now[0] += controller.settings.polling.primary.stale_after_seconds
    controller.tick()
    assert not controller.fresh and streams[1][1].is_set()
    wait_until(qapp, lambda: controller.telemetry.soc_pct == 40 and controller.fresh)

    now[0] += 30
    controller.tick()
    wait_until(qapp, lambda: len(streams) == 3)
    streams[2][0].put(report)
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    streams[2][2](decode_report(b'{"cmsBattSoc":99}'))
    controller.set_system_activity(SystemActivity(locked=True))
    qapp.processEvents()
    assert not controller.fresh and streams[2][1].is_set() and controller.telemetry.soc_pct == 80
    controller.set_system_activity(SystemActivity())
    wait_until(qapp, lambda: len(streams) == 4)
    assert not controller.fresh
    streams[3][0].put(report)
    wait_until(qapp, lambda: controller.fresh)


def test_saving_https_only_stops_active_mqtt_and_disables_probes(controller, mqtt_monitor, qapp):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    reports, cancelled, receive = streams[0]
    reports.put(b'{"cmsBattSoc":80,"powInSumW":0,"powOutSumW":65}')
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    controller.apply_settings(replace(controller.settings, language="uk"))
    assert controller.telemetry_source == "mqtt" and not cancelled.is_set()
    controller.apply_settings(replace(controller.settings, telemetry_transport="https_only"))
    assert controller.telemetry_source == "https" and cancelled.is_set() and not controller.fresh
    receive(decode_report(b'{"cmsBattSoc":99}'))
    controller.tick()
    wait_until(qapp, lambda: controller.fresh and controller.telemetry.soc_pct == 40)
    now[0] += 91
    controller.tick()
    wait_until(qapp, lambda: requests.count("quota") == 2 and "api" not in controller._active)
    assert requests.count("certification") == 1 and len(streams) == 1


@pytest.mark.parametrize("mqtt_monitor", [403], indirect=True)
def test_mqtt_certification_denial_still_allows_https_telemetry(controller, mqtt_monitor, qapp):
    _, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: controller.fresh)
    assert controller.telemetry_source == "https_fallback" and controller.telemetry.soc_pct == 40
    assert requests == ["list", "certification", "quota"] and not streams


@pytest.mark.parametrize("language,https_label", [("uk", "Лише HTTPS"), ("en", "HTTPS only")])
def test_transport_draft_applies_only_when_saved(controller, qapp, language, https_label):
    from ecoflow_monitor.ui.settings_window import SettingsWindow

    controller.apply_settings(replace(controller.settings, language=language))
    window = SettingsWindow(controller)
    try:
        index = window.telemetry_transport.findData("https_only")
        assert window.telemetry_transport.itemText(index) == https_label
        window.telemetry_transport.setCurrentIndex(index)
        assert window.gather(False).telemetry_transport == "https_only"
        assert controller.settings.telemetry_transport == "mqtt_with_https_fallback"
        window.load_draft()
        assert window.telemetry_transport.currentData() == "mqtt_with_https_fallback"
        window.telemetry_transport.setCurrentIndex(index)
        window.save()
        assert controller.store.load().telemetry_transport == "https_only"
    finally:
        window.close()


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
    controller.apply_settings(replace(controller.settings, serial="TEST", telemetry_transport="https_only"), Credentials("synthetic", "synthetic"))
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

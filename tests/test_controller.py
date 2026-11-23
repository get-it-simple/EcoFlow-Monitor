from dataclasses import replace
import json
import queue
import threading
import time

import httpx
import pytest
from qt_helpers import wait_until as wait_for_condition

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError
from ecoflow_monitor.adapters.ecoflow_mqtt import MqttError, decode_report
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.diagnostics import DiagnosticLog
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
def mqtt_certification_responses():
    return queue.Queue()


def mqtt_certification(host="mqtt.ecoflow.com", port="8883", password="synthetic-password"):
    return httpx.Response(200, json={"code": "0", "data": {
        "url": host, "port": port, "protocol": "mqtts",
        "certificateAccount": "synthetic-account", "certificatePassword": password}})


@pytest.fixture
def mqtt_monitor(controller, request, mqtt_certification_responses):
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
            try:
                response = mqtt_certification_responses.get_nowait()
            except queue.Empty:
                response = None
            if response is not None:
                return response() if callable(response) else response
            if certification_status != 200:
                return httpx.Response(certification_status)
            return mqtt_certification()
        assert name == "quota"
        waiting_https.set()
        assert release_https.wait(4)
        return httpx.Response(200, json={"code": "0", "data": {"powInSumW": 10, "powOutSumW": 20, "cmsBattSoc": 40}})

    def stream(access, serial, cancelled, receive):
        assert serial == "TEST"
        reports = queue.Queue()
        streams.append((reports, cancelled, receive, access))
        while not cancelled.is_set():
            try:
                report = reports.get(timeout=0.02)
            except queue.Empty:
                continue
            if isinstance(report, MqttError):
                raise report
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
    now[0] += 119
    reports = streams[0][0]
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

    reports.put(MqttError("stream_disconnected"))
    wait_until(qapp, lambda: controller.telemetry_source == "https_fallback" and controller.telemetry.soc_pct == 40)
    assert requests.count("quota") == 1
    now[0] += 5
    controller.tick()
    wait_until(qapp, lambda: len(streams) == 2 and "api" not in controller._active)
    assert requests.count("quota") == 1
    now[0] += 25
    waiting_https.clear()
    release_https.clear()
    controller.tick()
    wait_until(qapp, waiting_https.is_set)
    streams[1][0].put(b'{"cmsBattSoc":75,"powInSumW":0,"powOutSumW":65}')
    wait_until(qapp, lambda: controller.telemetry.soc_pct == 75)
    release_https.set()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.telemetry.soc_pct == 75 and controller.telemetry_source == "mqtt"
    assert requests.count("certification") == 1 and requests.count("quota") == 2


def test_mqtt_reconnect_backoff_is_bounded(controller, mqtt_monitor, qapp):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    for attempt, delay in enumerate((5, 10, 20, 40, 60, 60), start=1):
        _, cancelled, _, _ = streams[-1]
        streams[-1][0].put(MqttError("transport_failure"))
        wait_until(qapp, lambda: cancelled.is_set() and controller.fresh)
        assert controller.telemetry_source == "https_fallback"
        now[0] += delay - 1
        controller.tick()
        wait_until(qapp, lambda: "api" not in controller._active)
        assert len(streams) == attempt
        now[0] += 1
        controller.tick()
        wait_until(qapp, lambda: len(streams) == attempt + 1)
    assert requests.count("certification") == 1


def test_resume_restarts_mqtt_during_backoff_and_refreshes_https(controller, mqtt_monitor, qapp):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    report = b'{"cmsBattSoc":80,"powInSumW":0,"powOutSumW":65}'
    streams[0][0].put(report)
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    streams[0][0].put(MqttError("stream_disconnected"))
    wait_until(qapp, lambda: requests.count("quota") == 1 and controller.fresh)

    sleeping = SystemActivity().transition("suspend")
    controller.set_system_activity(sleeping)
    controller.set_system_activity(sleeping.transition("resume"))
    qapp.processEvents()
    assert len(streams) == 1 and requests.count("quota") == 1
    now[0] += 1
    controller.set_system_activity(sleeping.transition("resume_user"))
    wait_until(qapp, lambda: len(streams) == 2 and requests.count("quota") == 2 and controller.fresh)
    assert controller.telemetry_source == "https_fallback"
    assert streams[0][3] == streams[1][3] and requests.count("certification") == 1
    streams[1][0].put(report)
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    assert controller.telemetry.soc_pct == 80


@pytest.mark.parametrize("reason", ["stream_disconnected", "first_report_timeout"])
def test_mqtt_fallback_logs_the_attempt_and_correlates_recovery(controller, mqtt_monitor, qapp, tmp_path, reason):
    now, requests, streams, _, _ = mqtt_monitor
    path = tmp_path / "mqtt.log"
    with DiagnosticLog(path) as log:
        log.configure("normal")
        controller.refresh()
        wait_until(qapp, lambda: len(streams) == 1)
        if reason == "first_report_timeout":
            now[0] += 120
            controller.tick()
        else:
            streams[0][0].put(MqttError(reason))
        wait_until(qapp, lambda: controller.telemetry_source == "https_fallback" and controller.fresh)
        assert controller.telemetry.soc_pct == 40

        now[0] += 5
        controller.tick()
        wait_until(qapp, lambda: len(streams) == 2)
        streams[1][0].put(b'{"cmsBattSoc":75,"powInSumW":0,"powOutSumW":65}')
        wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
        assert controller.fresh and controller.telemetry.soc_pct == 75
        wait_until(qapp, lambda: "api" not in controller._active)
        quota_requests = requests.count("quota")
        now[0] += 31
        controller.tick()
        assert "api" not in controller._active and requests.count("quota") == quota_requests
        log.flush()

    names = {"mqtt_setup_started", "mqtt_fallback", "telemetry_source_changed"}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    first, fallback, second, active = [record for record in records if record["event"] in names]
    assert [record["event"] for record in (first, fallback, second, active)] == [
        "mqtt_setup_started", "mqtt_fallback", "mqtt_setup_started", "telemetry_source_changed",
    ]
    first_operation, second_operation = first["data"]["operation"], second["data"]["operation"]
    assert first_operation and second_operation and first_operation != second_operation
    assert fallback["data"] == {"operation": first_operation, "reason": reason, "retry_in_seconds": 5}
    assert active["data"] == {"operation": second_operation, "source": "mqtt"}
    accesses = [record["data"] for record in records if record["event"] == "mqtt_access_ready"]
    assert accesses == [
        {"operation": first_operation, "source": "api"},
        {"operation": second_operation, "source": "cache"},
    ]
    assert "synthetic-password" not in path.read_text(encoding="utf-8")


def test_mqtt_timeout_staleness_and_pause_reject_old_session_reports(controller, mqtt_monitor, qapp):
    now, requests, streams, waiting_https, release_https = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    now[0] += 119
    controller.tick()
    assert controller.telemetry_source == "waiting_mqtt" and not streams[0][1].is_set()
    now[0] += 1
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
    waiting_https.clear()
    release_https.clear()
    controller.set_system_activity(SystemActivity())
    wait_until(qapp, lambda: len(streams) == 4 and waiting_https.is_set())
    assert not controller.fresh and controller.telemetry.soc_pct == 80
    streams[3][0].put(report)
    wait_until(qapp, lambda: controller.fresh and controller.telemetry_source == "mqtt")
    release_https.set()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.telemetry.soc_pct == 80 and requests.count("certification") == 1


def test_saving_https_only_stops_active_mqtt_and_disables_probes(controller, mqtt_monitor, qapp):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    reports, cancelled, receive, _ = streams[0]
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
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: controller.fresh)
    assert controller.telemetry_source == "https_fallback" and controller.telemetry.soc_pct == 40
    assert requests == ["list", "certification", "quota"] and not streams
    now[0] += 301
    controller.tick()
    wait_until(qapp, lambda: requests.count("quota") == 2)
    assert requests.count("certification") == 1


@pytest.mark.parametrize("cached", [False, True])
def test_mqtt_authorization_rejection_allows_only_a_single_recovery(
    controller, mqtt_monitor, mqtt_certification_responses, qapp, cached,
):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    if cached:
        streams[0][0].put(MqttError("stream_disconnected"))
        wait_until(qapp, lambda: controller.fresh)
        now[0] += 5
        controller.tick()
        wait_until(qapp, lambda: len(streams) == 2)
        assert streams[1][3] == streams[0][3] and requests.count("certification") == 1
        streams[1][0].put(MqttError("broker_auth_rejected"))
        wait_until(qapp, lambda: streams[1][1].is_set())
        mqtt_certification_responses.put(mqtt_certification(password="new-synthetic-password"))
        now[0] += 10
        controller.tick()
        wait_until(qapp, lambda: len(streams) == 3)
        assert streams[2][3].password == "new-synthetic-password" and requests.count("certification") == 2
    streams[-1][0].put(MqttError("broker_auth_rejected"))
    wait_until(qapp, lambda: controller.mqtt_access_status == "mqtt_access_rejected")
    controller.tick()
    wait_until(qapp, lambda: controller.fresh)
    now[0] += 600
    controller.set_system_activity(SystemActivity(locked=True))
    qapp.processEvents()
    controller.set_system_activity(SystemActivity())
    wait_until(qapp, lambda: controller.fresh and "api" not in controller._active)
    assert controller.telemetry_source == "https_fallback" and controller.can_renew_mqtt_access
    assert requests.count("certification") == (2 if cached else 1)

    controller.renew_mqtt_access()
    wait_until(qapp, lambda: len(streams) == (4 if cached else 2))
    assert requests.count("certification") == (3 if cached else 2)


def test_manual_mqtt_renewal_reconnects_with_replacement_and_preserves_draft(
    controller, mqtt_monitor, mqtt_certification_responses, qapp,
):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from ecoflow_monitor.ui.settings_window import SettingsWindow

    _, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    streams[0][0].put(b'{"cmsBattSoc":75,"powInSumW":0,"powOutSumW":65}')
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    window = SettingsWindow(controller)
    release = threading.Event()

    def issue_replacement():
        assert release.wait(4)
        return mqtt_certification("mqtt-new.ecoflow.com", "9883", "replacement-password")

    try:
        window.telemetry_transport.setCurrentIndex(window.telemetry_transport.findData("https_only"))
        assert window.mqtt_renew_button.isEnabled() and not window.mqtt_renew_button.isHidden()
        mqtt_certification_responses.put(issue_replacement)
        QTest.keyClick(window.mqtt_renew_button, Qt.Key.Key_Space)
        wait_until(qapp, lambda: requests.count("certification") == 2)
        assert not window.mqtt_renew_button.isEnabled() and window.mqtt_renew_status.text()
        assert controller.telemetry_source == "mqtt" and not streams[0][1].is_set()

        release.set()
        wait_until(qapp, lambda: len(streams) == 2 and "api" not in controller._active)
        access = streams[1][3]
        assert (access.host, access.port, access.password) == ("mqtt-new.ecoflow.com", 9883, "replacement-password")
        assert window.gather(False).telemetry_transport == "https_only"
        assert controller.settings.telemetry_transport == "mqtt_with_https_fallback"
        assert controller.mqtt_access_status == "mqtt_access_renewed" and requests.count("certification") == 2
    finally:
        release.set()
        window.close()


def test_mqtt_manual_rate_limit_keeps_access_and_survives_pause(
    controller, mqtt_monitor, mqtt_certification_responses, qapp,
):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    streams[0][0].put(b'{"cmsBattSoc":75,"powInSumW":0,"powOutSumW":65}')
    wait_until(qapp, lambda: controller.telemetry_source == "mqtt")
    mqtt_certification_responses.put(httpx.Response(429, headers={"Retry-After": "90"}))
    controller.renew_mqtt_access()
    wait_until(qapp, lambda: controller.mqtt_access_status == "mqtt_access_renew_failed")
    assert controller.telemetry_source == "mqtt" and not streams[0][1].is_set()
    assert not controller.can_renew_mqtt_access
    controller.set_system_activity(SystemActivity(locked=True))
    qapp.processEvents()
    now[0] += 89
    controller.set_system_activity(SystemActivity())
    wait_until(qapp, lambda: controller.telemetry_source == "https_fallback" and controller.fresh)
    controller.renew_mqtt_access()
    assert requests.count("certification") == 2 and len(streams) == 1
    now[0] += 1
    controller.tick()
    wait_until(qapp, lambda: len(streams) == 2)
    assert requests.count("certification") == 2 and streams[1][3] == streams[0][3]


def test_late_mqtt_renewal_is_discarded_when_api_keys_change(
    controller, mqtt_monitor, mqtt_certification_responses, qapp,
):
    _, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    release = threading.Event()

    def delayed_replacement():
        assert release.wait(4)
        return mqtt_certification(password="obsolete-replacement")

    try:
        mqtt_certification_responses.put(delayed_replacement)
        controller.renew_mqtt_access()
        wait_until(qapp, lambda: requests.count("certification") == 2)
        mqtt_certification_responses.put(mqtt_certification(password="current-replacement"))
        controller.apply_settings(controller.settings, Credentials("different-access", "different-secret"))
        release.set()
        wait_until(qapp, lambda: len(streams) == 2)
        assert streams[1][3].password == "current-replacement"
        assert streams[0][1].is_set() and requests.count("certification") == 3
    finally:
        release.set()


def test_failed_mqtt_renewal_does_not_restore_access_rejected_during_request(
    controller, mqtt_monitor, mqtt_certification_responses, qapp,
):
    now, requests, streams, _, _ = mqtt_monitor
    controller.refresh()
    wait_until(qapp, lambda: len(streams) == 1)
    streams[0][0].put(MqttError("stream_disconnected"))
    wait_until(qapp, lambda: controller.fresh)
    now[0] += 5
    controller.tick()
    wait_until(qapp, lambda: len(streams) == 2 and controller.can_renew_mqtt_access)
    release = threading.Event()

    def delayed_rate_limit():
        assert release.wait(4)
        return httpx.Response(429, headers={"Retry-After": "120"})

    try:
        mqtt_certification_responses.put(delayed_rate_limit)
        controller.renew_mqtt_access()
        wait_until(qapp, lambda: requests.count("certification") == 2)
        streams[1][0].put(MqttError("broker_auth_rejected"))
        wait_until(qapp, lambda: streams[1][1].is_set())
        release.set()
        wait_until(qapp, lambda: controller.mqtt_access_status == "mqtt_access_renew_failed")
        mqtt_certification_responses.put(mqtt_certification(password="recovered-password"))
        now[0] += 120
        controller.tick()
        wait_until(qapp, lambda: len(streams) == 3)
        assert streams[2][3].password == "recovered-password" and requests.count("certification") == 3
    finally:
        release.set()


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
        assert window.mqtt_renew_button.isHidden() and not window.mqtt_renew_button.isEnabled()
    finally:
        window.close()


@pytest.mark.parametrize("language", ["en", "uk"])
def test_dashboard_displays_the_current_transport(controller, mqtt_monitor, qapp, language):
    from ecoflow_monitor.i18n import t
    from ecoflow_monitor.ui.dashboard import Dashboard

    now, _, streams, _, _ = mqtt_monitor
    controller.apply_settings(replace(controller.settings, language=language))
    window = Dashboard(controller, lambda: None)
    label = window.monitoring.updated
    try:
        assert label.text().endswith(t("never_updated"))
        controller.refresh()
        wait_until(qapp, lambda: len(streams) == 1)
        assert label.text().endswith(t("telemetry_waiting_mqtt"))
        streams[0][0].put(b'{"cmsBattSoc":80,"powInSumW":0,"powOutSumW":65}')
        wait_until(qapp, lambda: label.text().endswith("MQTT"))
        streams[0][0].put(MqttError("stream_disconnected"))
        wait_until(qapp, lambda: label.text().endswith(t("telemetry_fallback_short")))
        controller.apply_settings(replace(controller.settings, telemetry_transport="https_only"))
        assert label.text().endswith("HTTPS")
        controller.set_system_activity(SystemActivity(sleeping=True))
        wait_until(qapp, lambda: not label.text().endswith("HTTPS"))
        now[0] += 100
        controller.set_system_activity(SystemActivity())
        wait_until(qapp, lambda: label.text().endswith("HTTPS"))
    finally:
        window.deleteLater()


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

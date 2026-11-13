from dataclasses import replace
import json
import time
import threading

import httpx
from PySide6.QtTest import QTest
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, normalize_partial
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.integrations import IntegrationRule
from ecoflow_monitor.polling import CORE, PollingGroup, PollingPreferences, PollingState, SENSOR_FIELDS, TelemetryPatch
from ecoflow_monitor.settings import AppSettings, SettingsStore


def complete(state, values, now, missing=()):
    state.complete(TelemetryPatch(tuple((*values, *missing)), tuple(values.items()), tuple(missing)), 1800000000 + now, now)


def wait(qapp, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert predicate()


def test_signed_port_values_survive_partial_normalization_and_snapshot():
    fields = ("powGetAcOut", "powGetQcusb1", "powOutSumW")
    patch = normalize_partial({"powGetAcOut": -420, "powGetQcusb1": -2, "powOutSumW": 422}, fields)
    state = PollingState(PollingPreferences())
    state.complete(patch, 100, 10)
    snapshot = state.snapshot(None, 100, 10)
    assert dict(snapshot.ports) == {"ac_output": -420, "usb_a1": -2}
    assert snapshot.output_w == 422 and snapshot.input_w is None
    assert not patch.invalid


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), {}, []])
def test_partial_normalization_rejects_invalid_signed_port_values(value):
    patch = normalize_partial({"powGetAcOut": value}, ("powGetAcOut",))
    assert patch.values == () and patch.invalid == ("ac_output",)


def test_independent_ages_and_omitted_fields_do_not_become_fresh():
    state = PollingState(PollingPreferences())
    complete(state, {"battery_level": 75, "ac_output": -420}, 0)
    complete(state, {"battery_level": 74}, 100)
    assert state.fresh({"battery_level"}, 181)
    assert not state.fresh({"ac_output"}, 181)
    snapshot = state.snapshot(None, 10100, 100)
    assert dict(snapshot.ports)["ac_output"] == -420
    assert state.samples["ac_output"].received_monotonic == 0
    complete(state, {}, 190, missing=("ac_output",))
    assert state.samples["ac_output"].received_monotonic == 0
    assert state.samples["ac_output"].value == -420
    assert state.presentation({"ac_output"}, 190) == "missing"


def test_groups_are_combined_only_when_due_and_bootstrap_is_once():
    state = PollingState(PollingPreferences())
    keys = CORE | {"ac_output", "minimum_discharge_level"}
    state.set_demand(keys, keys, 0)
    assert set(state.begin(0)) == {SENSOR_FIELDS[key] for key in keys}
    complete(state, {"input_power": 0, "output_power": 420, "battery_level": 75, "ac_output": -420, "minimum_discharge_level": 10}, 0)
    assert state.due(29) == frozenset()
    assert state.due(30) == CORE
    assert state.due(60) == CORE | {"ac_output"}
    assert state.due(300) == keys
    state.set_demand({"minimum_discharge_level"}, {"minimum_discharge_level"}, 301)
    assert set(state.begin(301)) == {"cmsMinDsgSoc"}


def test_loading_is_local_and_resuming_fresh_values_has_no_indicator():
    state = PollingState(PollingPreferences())
    state.verified = True
    state.set_demand({"battery_level"}, {"battery_level"}, 0)
    assert state.presentation({"battery_level"}, 0) == "loading"
    state.begin(0)
    complete(state, {"battery_level": 75}, 0)
    state.set_demand((), (), 5)
    state.set_demand({"battery_level"}, {"battery_level"}, 10)
    assert state.presentation({"battery_level"}, 10) == "ready"
    state.begin(30)
    assert state.presentation({"battery_level"}, 30) == "ready"
    complete(state, {"battery_level": 74}, 31)
    state.set_demand((), (), 35)
    state.set_demand({"battery_level"}, {"battery_level"}, 65)
    state.begin(65)
    assert state.presentation({"battery_level"}, 65) == "refreshing"
    complete(state, {"battery_level": 73}, 66)
    state.set_demand((), (), 70)
    state.set_demand({"battery_level"}, {"battery_level"}, 160)
    assert state.presentation({"battery_level"}, 160) == "loading"
    state.begin(160)
    state.fail(170)
    assert state.presentation({"battery_level"}, 170) == "stale"


def test_missing_sensor_waits_for_its_interval_instead_of_busy_retrying():
    state = PollingState(PollingPreferences())
    state.verified = True
    state.set_demand({"usb_a1"}, {"usb_a1"}, 0)
    state.begin(0)
    complete(state, {}, 1, ("usb_a1",))
    assert state.presentation({"usb_a1"}, 2) == "missing"
    assert state.due(60) == frozenset()
    assert state.due(61) == {"usb_a1"}


@pytest.mark.parametrize("group", [PollingGroup(4, 90), PollingGroup(3601, 4000), PollingGroup(True, 90), PollingGroup(30, 29), PollingGroup(30, 86401), PollingGroup(30, 90.5)])
def test_invalid_group_preferences_are_rejected(group):
    with pytest.raises(ValueError):
        replace(AppSettings(), polling=PollingPreferences(primary=group)).validate()


def test_optional_preferences_migrate_and_roundtrip():
    old = AppSettings().to_dict()
    old["ecoflow"].pop("polling")
    loaded = AppSettings.from_dict(old)
    assert loaded.polling == PollingPreferences()
    changed = replace(loaded, polling=PollingPreferences(primary=PollingGroup(10, 40)))
    assert AppSettings.from_dict(changed.to_dict()) == changed


@pytest.fixture
def sensor_controller(tmp_path, qapp, session_cache):
    now = [1000.0]
    calls = []
    values = {"powInSumW": 0, "powOutSumW": 420, "cmsBattSoc": 75, "cmsMinDsgSoc": 10,
              "cmsMaxChgSoc": 90, "powGetAcOut": -420, "powGetQcusb1": -2,
              "flowInfoAcOut": 14, "flowInfo12v": 4, "cmsChgDsgState": 1, "cmsDsgRemTime": 120, "cmsChgRemTime": 0}
    failures = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "online": 1}]})
        quotas = json.loads(request.content)["params"]["quotas"]
        calls.append(quotas)
        if failures:
            raise failures.pop()
        return httpx.Response(200, json={"code": "0", "data": {key: values.get(key, 0) for key in quotas}})
    class MemoryCredentials:
        def read(self, reference):
            return Credentials("synthetic", "synthetic")
    controller = Controller(SettingsStore(tmp_path / "settings.json"), credential_store=MemoryCredentials(),
                            client_factory=lambda credentials: EcoFlowClient(credentials, httpx.MockTransport(handle)),
                            clock=lambda: now[0], credential_cache=session_cache)
    controller.settings = replace(controller.settings, serial="TEST")
    controller.set_sensor_demand("dashboard", ())
    yield controller, now, calls, values, failures
    controller.stop()
    qapp.processEvents()
    i18n.setup("en")


def test_controller_requests_visible_sensors_and_calculation_dependencies(sensor_controller, qapp):
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("widget", {"battery_level"})
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    sampling_fields = {"powInSumW", "powOutSumW", "cmsBattSoc", "cmsChgDsgState", "cmsDsgRemTime", "cmsChgRemTime"}
    assert set(calls[-1]) == sampling_fields
    now[0] += 31
    controller.tick()
    wait(qapp, lambda: "api" not in controller._active)
    assert set(calls[-1]) == sampling_fields
    controller.set_sensor_demand("widget", {"estimated_runtime"})
    controller.tick()
    wait(qapp, lambda: "api" not in controller._active)
    assert set(calls[-1]) == {"cmsMinDsgSoc"}
    assert controller.runtime is not None
    controller.set_sensor_demand("widget", ())
    count = len(calls)
    now[0] += 600
    controller.tick()
    qapp.processEvents()
    assert len(calls) == count


def test_hidden_auto_requirements_are_minimal(sensor_controller, qapp):
    controller, now, calls, values, failures = sensor_controller
    module = replace(controller.settings.integrations[0], enabled=True, auto_enabled=True,
                     system_rules=(IntegrationRule("low", "profile_5", max_soc=20),))
    controller.settings = replace(controller.settings, integrations=(module,))
    controller._update_demand()
    assert controller.polling.required == {"battery_level"}
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    now[0] += 31
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    assert set(calls[-1]) == {"powInSumW", "powOutSumW", "cmsBattSoc", "cmsChgDsgState", "cmsDsgRemTime", "cmsChgRemTime"}


def test_timeout_retains_values_and_retries_without_renewing_credentials(sensor_controller, qapp, monkeypatch):
    controller, now, calls, values, failures = sensor_controller
    monkeypatch.setattr("ecoflow_monitor.controller.internet_unavailable", lambda: False)
    controller.set_sensor_demand("widget", {"battery_level"})
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    stamp = controller.polling.samples["battery_level"].received_monotonic
    now[0] += 31
    failures.append(httpx.ReadTimeout("synthetic"))
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    assert controller.connection == ConnectionState.API_ERROR
    assert controller.telemetry.soc_pct == 75
    assert controller.polling.samples["battery_level"].received_monotonic == stamp
    assert controller._next_api == now[0] + 5
    now[0] += 5
    controller.tick()
    wait(qapp, lambda: "api" not in controller._active)
    assert controller.connection == ConnectionState.LIVE
    assert controller.polling.samples["battery_level"].received_monotonic == now[0]


def test_frequency_save_keeps_active_state_and_cache(sensor_controller, qapp):
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("widget", {"battery_level"})
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    generation, snapshot, polling = controller._generation, controller.telemetry, controller.polling
    controller.apply_settings(replace(controller.settings, polling=PollingPreferences(primary=PollingGroup(10, 40))))
    assert controller._generation == generation and controller.telemetry is snapshot and controller.polling is polling
    assert controller._next_api == now[0] + 10


def test_optional_missing_response_ends_loading_without_refreshing_old_values(sensor_controller, qapp):
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("widget", {"battery_level"})
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    stamp = controller.telemetry.received_monotonic
    now[0] += 10
    controller.set_sensor_demand("widget", {"ac_output"})
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "online": 1}]})
        return httpx.Response(200, json={"code": "0", "data": {}})
    controller.client_factory = lambda credentials: EcoFlowClient(credentials, httpx.MockTransport(handle))
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    assert controller.reading_state("ac_output") == "missing"
    assert controller.telemetry.received_monotonic == stamp
    assert not controller.fresh_for({"ac_output"})
    assert controller.fresh_for({"battery_level"})


def test_stale_view_dependency_does_not_disable_fresh_rule_inputs(sensor_controller, qapp):
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("widget", {"battery_level", "input_power"})
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    complete(controller.polling, {"battery_level": 74}, now[0] + 95)
    now[0] += 95
    controller.connection = ConnectionState.STALE
    assert not controller.fresh
    assert controller.fresh_for({"battery_level"})
    assert not controller.fresh_for({"input_power"})


def test_authentication_pause_does_not_leave_new_sensors_loading_forever(sensor_controller):
    controller, now, calls, values, failures = sensor_controller
    controller.connection = ConnectionState.AUTH_ERROR
    controller.set_sensor_demand("widget", {"battery_level"})
    assert controller.reading_state("battery_level") == "missing"


def test_abandoned_request_releases_its_fields_for_the_next_generation():
    state = PollingState(PollingPreferences())
    state.set_demand(CORE, CORE, 0)
    assert state.begin(0)
    state.abandon()
    assert not state.in_flight
    assert state.due(1) == CORE


def test_visibility_change_during_request_is_picked_up_by_the_next_request(sensor_controller, qapp):
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("widget", {"battery_level"})
    controller.refresh()
    wait(qapp, lambda: "api" not in controller._active)
    started, release = threading.Event(), threading.Event()
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "online": 1}]})
        quotas = json.loads(request.content)["params"]["quotas"]
        calls.append(quotas)
        started.set()
        release.wait(3)
        return httpx.Response(200, json={"code": "0", "data": {key: values[key] for key in quotas}})
    controller.client_factory = lambda credentials: EcoFlowClient(credentials, httpx.MockTransport(handle))
    now[0] += 31
    controller.refresh()
    try:
        wait(qapp, started.is_set)
        controller.set_sensor_demand("widget", {"battery_level", "minimum_discharge_level"})
        assert controller.reading_state("minimum_discharge_level") == "loading"
        release.set()
        wait(qapp, lambda: "api" not in controller._active)
        controller.tick()
        wait(qapp, lambda: "api" not in controller._active)
        sampling_fields = {"powInSumW", "powOutSumW", "cmsBattSoc", "cmsChgDsgState", "cmsDsgRemTime", "cmsChgRemTime"}
        assert [set(call) for call in calls[-2:]] == [sampling_fields, {"cmsMinDsgSoc"}]
        assert controller.telemetry.min_soc == 10
    finally:
        release.set()


@pytest.mark.design
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
@pytest.mark.parametrize("clear", [False, True])
def test_loading_marker_stays_inside_the_reading(sensor_controller, qapp, scale, clear):
    from ecoflow_monitor.ui.readings import MetricReading
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("widget", {"battery_level"})
    reading = MetricReading(controller, "battery_level")
    try:
        reading.configure(scale, clear=clear)
        reading.resize(round(200 * scale), round(100 * scale))
        reading.show()
        qapp.processEvents()
        image = reading.grab().toImage()
        assert reading.loader.isVisible()
        assert reading.value.rect().contains(reading.loader.geometry())
        assert not image.isNull()
        assert reading.value.outlined == clear
        complete(controller.polling, {"battery_level": 75}, now[0] - 40)
        controller.polling.set_demand((), (), now[0] - 1)
        controller.polling.set_demand({"battery_level"}, {"battery_level"}, now[0])
        controller.polling.verified = True
        controller.polling.begin(now[0])
        reading.refresh()
        assert reading.loader.parentWidget() is reading
        assert not reading.loader.geometry().intersects(reading.value.geometry())
        controller.settings = replace(controller.settings, animations=False)
        reading.motion.refresh()
        assert not reading.motion._timer.isActive()
    finally:
        reading.close()
        reading.deleteLater()


def test_selection_is_evaluated_after_the_device_check():
    now = [0]
    state = PollingState(PollingPreferences())
    state.verified = True
    complete(state, {"battery_level": 75, "ac_output": -420}, 0)
    state.set_demand({"battery_level", "ac_output"}, {"battery_level", "ac_output"}, 59)
    def handle(request):
        if request.method == "GET":
            now[0] = 60
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "online": 1}]})
        assert set(json.loads(request.content)["params"]["quotas"]) == {"cmsBattSoc", "powGetAcOut"}
        return httpx.Response(200, json={"code": "0", "data": {"cmsBattSoc": 74, "powGetAcOut": -421}})
    patch = EcoFlowClient(Credentials("synthetic", "synthetic"), httpx.MockTransport(handle)).read_quotas("TEST", lambda: state.begin(now[0]))
    assert dict(patch.values) == {"battery_level": 74, "ac_output": -421}


def test_settings_draft_and_loader_use_real_controls(sensor_controller, qapp):
    from ecoflow_monitor.ui.settings_window import SettingsWindow
    from ecoflow_monitor.ui.readings import MetricReading
    controller, now, calls, values, failures = sensor_controller
    window = SettingsWindow(controller)
    reading = MetricReading(controller, "battery_level")
    try:
        window.polling_fields["primary"][0].setValue(15)
        window._draft_changed()
        assert window.save_button.isEnabled()
        assert controller.settings.polling.primary.interval_seconds == 30
        window.save()
        assert controller.settings.polling.primary.interval_seconds == 15
        assert controller.store.load().polling == controller.settings.polling
        window.polling_fields["primary"][1].setValue(5)
        window._draft_changed()
        assert not window.save_button.isEnabled()
        window.load_draft()
        assert window.polling_fields["primary"][1].value() == 90
        controller.set_sensor_demand("widget", {"battery_level"})
        reading.resize(180, 90)
        reading.show()
        reading.refresh()
        qapp.processEvents()
        assert reading.loader.isVisible()
        assert "75" not in reading.value.text()
        controller.refresh()
        wait(qapp, lambda: "api" not in controller._active)
        reading.refresh()
        assert not reading.loader.isVisible()
        assert reading.value.text() == "75\u00a0%"
    finally:
        reading.close()
        window.close()
        reading.deleteLater()
        window.deleteLater()


def test_actual_widget_composition_controls_demand(sensor_controller, qapp):
    from ecoflow_monitor.application import Application
    controller, now, calls, values, failures = sensor_controller
    runtime = Application(controller.store.path, native=False, controller=controller)
    try:
        runtime.controller.update_widget(enabled=True, expanded_metrics=("minimum_discharge_level",),
                                         large_metrics=tuple(key for key in controller.settings.widget.large_metrics if key != "minimum_discharge_level"))
        runtime.widget.resize(240, 56)
        qapp.processEvents()
        runtime.sensor_demand.refresh()
        visible = set(runtime.widget.composition.visible)
        assert controller.polling.visible == visible | ({"charging_state"} if "battery_level" in visible else set())
        assert "minimum_discharge_level" not in controller.polling.required
        runtime.preview(replace(controller.settings, widget=replace(controller.settings.widget, small_metrics=("output_power",))))
        qapp.processEvents()
        assert controller.polling.visible == {"output_power"}
        runtime.preview(None)
        qapp.processEvents()
        runtime.dashboard.show()
        runtime.sensor_demand.refresh()
        assert "usb_a1" in controller.polling.required
        runtime.dashboard.hide()
        runtime.widget.reveal_button.setChecked(True)
        qapp.processEvents()
        runtime.sensor_demand.refresh()
        assert "minimum_discharge_level" in controller.polling.required
        runtime.widget.reveal_button.setChecked(False)
        runtime.sensor_demand.refresh()
        assert "minimum_discharge_level" not in controller.polling.required
    finally:
        runtime.stop()
        runtime.dashboard.deleteLater()
        runtime.widget.deleteLater()


def test_outlet_state_waits_independently_of_port_power(sensor_controller, qapp):
    from ecoflow_monitor.ui.monitoring import MonitoringPage
    controller, now, calls, values, failures = sensor_controller
    controller.set_sensor_demand("dashboard", {"ac_output", "ac_enabled"})
    complete(controller.polling, {"ac_output": -420}, now[0])
    controller.telemetry = controller.polling.snapshot(None, time.time(), now[0])
    controller.connection = ConnectionState.LIVE
    page = MonitoringPage(controller, lambda: None)
    try:
        page.resize(1000, 800)
        page.show()
        qapp.processEvents()
        page.refresh()
        row, value = page.port_values["ac_output"]
        assert "-420" in value.text()
        assert row.state_loader.isVisible()
        assert not row.output_state.isVisible()
        complete(controller.polling, {"ac_enabled": True}, now[0])
        controller.telemetry = controller.polling.snapshot(controller.telemetry, time.time(), now[0])
        page.refresh()
        assert not row.state_loader.isVisible()
        assert row.output_state.isVisible() and row.output_state.isChecked()
        complete(controller.polling, {"input_power": 420, "output_power": 422, "battery_level": 75}, now[0] - 100)
        controller.telemetry = controller.polling.snapshot(controller.telemetry, time.time(), now[0])
        page.refresh()
        assert page.balance_caption.text() == i18n.t("missing_data")
    finally:
        page.close()
        page.deleteLater()

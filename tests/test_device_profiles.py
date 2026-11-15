from dataclasses import replace
import json
from pathlib import Path
import threading

import httpx
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QScreen
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QMessageBox
import pytest
from qt_helpers import wait_until

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.ecoflow import Credentials, Device, EcoFlowClient, normalize_partial
from ecoflow_monitor.adapters.onepassword import OnePasswordError
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.device_profiles import BASELINE, CORE, DeviceProfile, ProfileError, baseline_profile, builtin_catalog, load_catalog, parse_catalog, write_example
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.polling import PollingPreferences, PollingState
from ecoflow_monitor.settings import AppSettings, SettingsStore
from ecoflow_monitor.ui.monitoring import MonitoringPage
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui.dashboard import Dashboard


def profile_data(**changes):
    result = {
        "id": "custom", "name": "Synthetic model", "revision": 1,
        "read": {"method": "POST", "path": "/iot-open/sign/device/quota"},
        "fields": {
            "input_power": {"sources": ["input"]}, "output_power": {"sources": ["output"]},
            "battery_level": {"sources": ["soc"]},
        },
    }
    result.update(changes)
    return result


def save_profile(directory, data, filename="custom.json"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps({"version": 1, "profiles": [data]}), encoding="utf-8")
    return path


class SyntheticCredentials:
    def read(self, reference):
        return Credentials("synthetic-access", "synthetic-secret")


@pytest.fixture
def controller(tmp_path, qapp, session_cache):
    instance = Controller(SettingsStore(tmp_path / "settings.json"), credential_store=SyntheticCredentials(), credential_cache=session_cache)
    instance.settings = replace(instance.settings, serial="TEST", device_profile_assignments=(("TEST", BASELINE),))
    yield instance
    instance.stop()
    qapp.processEvents()


def test_bundled_catalog_has_only_working_reference_and_eight_unverified_examples():
    catalog = builtin_catalog()
    assert [profile.reference for profile in catalog.profiles] == [BASELINE]
    assert baseline_profile().evidence == "working"
    assert len(catalog.examples) == 8
    assert all(example["evidence"] == "documentation" for example in catalog.examples)
    incomplete = {example["id"] for example in catalog.examples if not DeviceProfile.from_dict(example, "user").usable}
    assert incomplete == {"deltamax", "river2pro"}


def test_mapping_distinguishes_literal_dotted_keys_nested_paths_scaling_sum_and_arrays():
    data = profile_data()
    data["fields"].update({
        "input_power": {"sources": ["ac.power", ["pv", "power"]], "quotas": ["ac.power", "pv.power"], "scale": 0.1},
        "output_power": {"sources": [["ports", 0, "total"]], "quotas": ["ports.total"]},
        "anderson": {"sources": ["anderson"], "scale": 0.1},
        "ac_enabled": {"sources": ["ac"], "enum": {"0": False, "2": True}},
        "dc_enabled": {"sources": ["dc"], "enum": {"4": False}, "otherwise": True},
        "charging_state": {"sources": ["state"], "enum": {"10": 0, "20": 1, "30": 2}},
    })
    profile = DeviceProfile.from_dict(data, "user")
    raw = {"ac.power": "0", "ac": 2, "pv": {"power": 125}, "ports": [{"total": 0}], "soc": 100,
           "anderson": [-20, 0], "dc": 4, "state": 30}
    patch = normalize_partial(raw, (*CORE, "anderson", "ac_enabled", "dc_enabled", "charging_state"), profile)
    assert dict(patch.values) == {"input_power": 12.5, "output_power": 0, "battery_level": 100,
                                  "anderson": (-2, 0), "ac_enabled": True, "dc_enabled": False, "charging_state": 2}
    assert not patch.missing and not patch.invalid
    raw["pv"] = {}
    raw["soc"] = 101
    patch = normalize_partial(raw, tuple(CORE), profile)
    assert patch.missing == ("input_power",) and patch.invalid == ("battery_level",)
    assert dict(patch.values) == {"output_power": 0}


@pytest.mark.parametrize("value", [None, True, [], {}, -1, "nan", "inf"])
def test_custom_core_values_never_become_fabricated_zero(value):
    profile = DeviceProfile.from_dict(profile_data(), "user")
    patch = normalize_partial({"input": value, "output": 0, "soc": 50}, tuple(CORE), profile)
    assert patch.invalid == ("input_power",)
    assert dict(patch.values) == {"output_power": 0, "battery_level": 50}


@pytest.mark.parametrize("read", [
    {"method": "PUT", "path": "/iot-open/sign/device/quota"},
    {"method": "POST", "path": "https://other.example/read"},
    {"method": "POST", "path": "/iot-open/sign/device/set"},
    {"method": "GET", "path": "//other.example/read"},
])
def test_profiles_cannot_change_host_or_request_station_writes(read):
    with pytest.raises(ProfileError, match="device_profile_endpoint_error"):
        DeviceProfile.from_dict(profile_data(read=read), "user")


def test_model_identification_requires_unique_explicit_id_and_never_uses_name():
    first = DeviceProfile.from_dict(profile_data(model_ids=["official-model-1"]), "user")
    catalog = replace(builtin_catalog(), profiles=(*builtin_catalog().profiles, first))
    assert catalog.resolve("", "official-model-1") == first
    assert catalog.resolve("", "Synthetic model") is None
    assert catalog.resolve("", "cmsBattSoc") is None
    second = replace(first, id="duplicate-model")
    assert replace(catalog, profiles=(*catalog.profiles, second)).resolve("", "official-model-1") is None


def test_disabled_custom_files_are_not_read_and_override_requires_explicit_reference(tmp_path):
    save_profile(tmp_path, profile_data(id="delta3max", evidence="working"))
    disabled = load_catalog(tmp_path, False)
    assert disabled.resolve("user:delta3max") is None
    enabled = load_catalog(tmp_path, True)
    assert enabled.resolve(BASELINE) == baseline_profile()
    assert enabled.resolve("user:delta3max").evidence == "custom"
    assert enabled.resolve("") is None


def test_invalid_and_duplicate_files_do_not_replace_bundled_or_independent_profiles(tmp_path):
    save_profile(tmp_path, profile_data(), "one.json")
    save_profile(tmp_path, profile_data(), "two.json")
    save_profile(tmp_path, profile_data(id="independent"), "three.json")
    (tmp_path / "broken.json").write_text("{broken", encoding="utf-8")
    catalog = load_catalog(tmp_path, True)
    assert {profile.reference for profile in catalog.profiles} == {BASELINE, "user:independent"}
    assert set(catalog.errors) == {("one.json", "device_profile_duplicate"), ("two.json", "device_profile_duplicate"), ("broken.json", "device_profile_invalid")}
    with pytest.raises(ProfileError, match="device_profile_duplicate"):
        parse_catalog('{"version":1,"version":1,"profiles":[]}', "user")


@pytest.mark.parametrize("change", [
    {"scale": 10 ** 400}, {"minimum": 10 ** 400}, {"sources": [["input", -1]], "quotas": ["input"]},
    {"sources": ["input"], "execute": "anything"},
])
def test_invalid_mapping_is_a_reported_profile_error(change):
    data = profile_data()
    data["fields"]["input_power"].update(change)
    with pytest.raises(ProfileError):
        parse_catalog(json.dumps({"version": 1, "profiles": [data]}), "user")


def test_examples_are_explicit_copies_and_existing_files_are_preserved(tmp_path):
    destination = write_example(tmp_path, "delta2")
    original = destination.read_bytes()
    assert load_catalog(tmp_path, True).resolve("user:delta2").evidence == "documentation"
    with pytest.raises(ProfileError, match="device_profile_write_error"):
        write_example(tmp_path, "delta2")
    assert destination.read_bytes() == original


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_profile_controls_request_and_only_due_internal_sensors_gain_freshness(method):
    endpoint = "/iot-open/sign/device/quota/all" if method == "GET" else "/iot-open/sign/device/quota"
    profile = DeviceProfile.from_dict(profile_data(read={"method": method, "path": endpoint}), "user")
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == method and request.url.path == endpoint
        assert request.url.host == "api.ecoflow.com"
        if method == "POST":
            assert json.loads(request.content) == {"sn": "TEST", "params": {"quotas": ["soc"]}}
        else:
            assert dict(request.url.params) == {"sn": "TEST"}
        return httpx.Response(200, json={"code": "0", "data": {"soc": 50, "input": 900, "output": 100}})
    client = EcoFlowClient(Credentials("synthetic", "synthetic"), httpx.MockTransport(handle))
    client.catalog = (Device("TEST", "Synthetic", True),)
    client.profile = profile
    state = PollingState(PollingPreferences())
    state.verified = True
    state.set_demand({"battery_level"}, {"battery_level"}, 0)
    patch = client.read_quotas("TEST", lambda: state.begin(0))
    state.complete(patch, 100, 0)
    assert set(state.samples) == {"battery_level"}
    assert state.samples["battery_level"].value == 50
    assert len(requests) == 1


def test_unknown_profile_diagnostics_only_reads_device_list():
    requests, captured = [], []
    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"code": "0", "data": []})
    client = EcoFlowClient(Credentials("synthetic", "synthetic"), httpx.MockTransport(handle))
    client.profile = None
    assert client.diagnose("TEST", lambda event, **fields: captured.append((event, fields))) == ()
    assert requests == ["/iot-open/sign/device/list"]
    assert any(event == "device_diagnostic_profile_missing" for event, fields in captured)


def test_old_settings_migrate_current_station_but_new_stations_require_selection():
    payload = replace(AppSettings(), serial="EXISTING").to_dict()
    payload["ecoflow"].pop("device_support")
    migrated = AppSettings.from_dict(payload)
    assert migrated.device_profile_assignments == (("EXISTING", BASELINE),)
    assert not migrated.custom_device_profiles
    new = replace(migrated, serial="NEW")
    assert dict(new.device_profile_assignments).get("NEW") is None
    assert AppSettings.from_dict(new.to_dict()) == new


def test_unassigned_device_is_not_probed_with_delta3max_mapping(controller, qapp):
    controller.settings = replace(controller.settings, device_profile_assignments=())
    requests = []
    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "deviceName": "DELTA 3 MAX", "online": 1}]})
    controller.client_factory = lambda credentials: EcoFlowClient(credentials, httpx.MockTransport(handle))
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.connection == ConnectionState.UNSUPPORTED
    assert requests == ["/iot-open/sign/device/list"]


def test_profile_reload_is_staged_and_saving_new_mapping_clears_old_samples(controller, qapp):
    data = profile_data()
    save_profile(controller.profile_directory, data)
    initial = load_catalog(controller.profile_directory, True)
    settings = replace(controller.settings, custom_device_profiles=True, device_profile_assignments=(("TEST", "user:custom"),))
    controller.apply_settings(settings, profile_catalog=initial)
    controller.polling.complete(normalize_partial({"soc": 50}, ("battery_level",), initial.resolve("user:custom")), 1800000000, 0)
    window = SettingsWindow(controller)
    try:
        data["revision"] = 2
        data["fields"]["battery_level"] = {"sources": ["newSoc"]}
        save_profile(controller.profile_directory, data)
        window.device_profiles.scan(manual=True)
        wait_until(qapp, lambda: not window.device_profiles.busy)
        assert controller.profile_catalog == initial
        assert controller.polling.samples["battery_level"].value == 50
        assert window.device_profiles.catalog_changed
        generation = controller._generation
        window.save()
        assert controller._generation == generation + 1
        assert controller.profile_catalog.resolve("user:custom").revision == 2
        assert not controller.polling.samples and controller.telemetry is None
    finally:
        window.reject()


def test_metric_tooltip_uses_the_active_profile_and_omits_unmapped_fields(controller):
    from ecoflow_monitor.ui.metrics import metric_tooltip
    profile = DeviceProfile.from_dict(profile_data(), "user")
    controller.profile_catalog = replace(builtin_catalog(), profiles=(profile,), custom_enabled=True)
    controller.settings = replace(controller.settings, custom_device_profiles=True, device_profile_assignments=(("TEST", profile.reference),))
    assert i18n.t("metric_api_field").format(field="soc") in metric_tooltip("battery_level", controller)
    assert "cmsBattSoc" not in metric_tooltip("battery_level", controller)
    assert "powGetAcOut" not in metric_tooltip("ac_output", controller)


def test_additional_profiles_require_modal_confirmation_each_enable_and_cancel_is_staged(controller, qapp):
    window = SettingsWindow(controller)
    window.show()
    try:
        panel = window.device_profiles
        assert window.connection_tabs.currentIndex() == 0
        assert not panel.support.header.isChecked()
        panel.enabled.setChecked(True)
        prompt = panel._confirmation
        assert isinstance(prompt, QMessageBox) and prompt.isModal()
        assert not panel.enabled.isChecked()
        prompt.button(QMessageBox.StandardButton.Yes).click()
        wait_until(qapp, lambda: not panel.busy)
        assert panel.enabled.isChecked() and not controller.settings.custom_device_profiles
        panel.enabled.setChecked(False)
        panel.enabled.setChecked(True)
        prompt = panel._confirmation
        assert isinstance(prompt, QMessageBox)
        prompt.button(QMessageBox.StandardButton.No).click()
        qapp.processEvents()
        assert not panel.enabled.isChecked() and not controller.store.path.exists()
    finally:
        window.reject()


def test_onepassword_references_are_masked_and_create_fields_are_independent(controller):
    controller.settings = replace(controller.settings, credential_source="1password", onepassword_access_ref="op://Test/Item/username", onepassword_secret_ref="op://Test/Item/password")
    window = SettingsWindow(controller)
    try:
        panel = window.onepassword
        assert panel.access_ref.echoMode() == panel.secret_ref.echoMode() == QLineEdit.EchoMode.Password
        panel.show_references.setChecked(True)
        assert panel.access_ref.echoMode() == QLineEdit.EchoMode.Normal
        panel.create_access.setText("new-access")
        panel.create_secret.setText("new-secret")
        assert window.access.text() == window.secret.text() == ""
        panel.source.setCurrentIndex(0)
        assert panel.access_ref.echoMode() == QLineEdit.EchoMode.Password
        assert panel.create_access.text() == panel.create_secret.text() == ""
        assert window.gather().onepassword_access_ref == "op://Test/Item/username"
        panel.source.setCurrentIndex(1)
        panel.access_ref.setText("incomplete draft")
        panel.source.setCurrentIndex(0)
        assert window.gather().onepassword_access_ref == "op://Test/Item/username"
    finally:
        window.reject()


def test_navigation_masks_previously_revealed_onepassword_references(controller, qapp):
    controller.settings = replace(controller.settings, credential_source="1password", onepassword_access_ref="op://Test/Item/username", onepassword_secret_ref="op://Test/Item/password")
    window = SettingsWindow(controller)
    window.connection_tabs.setCurrentIndex(1)
    window.show()
    try:
        qapp.processEvents()
        window.onepassword.show_references.setChecked(True)
        assert window.onepassword.access_ref.echoMode() == QLineEdit.EchoMode.Normal
        window.connection_tabs.setCurrentIndex(0)
        window.connection_tabs.setCurrentIndex(1)
        qapp.processEvents()
        assert window.onepassword.access_ref.echoMode() == QLineEdit.EchoMode.Password
        assert not window.onepassword.show_references.isChecked()
    finally:
        window.reject()


def test_explicit_renewal_after_recent_auth_failure_does_not_wait_for_poll_interval(controller, qapp, monkeypatch):
    controller.settings = replace(controller.settings, credential_source="1password", onepassword_access_ref="op://Test/Item/username", onepassword_secret_ref="op://Test/Item/password")
    attempts = []
    def load(*args):
        attempts.append(True)
        return Credentials("retry-access", "retry-secret")
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", load)
    state = controller.polling
    state.begin(controller.clock())
    state.fail(controller.clock())
    controller.connection = ConnectionState.AUTH_ERROR
    assert not state.due(controller.clock())
    class Client:
        def read_quotas(self, serial, select, **kwargs):
            return normalize_partial({"powInSumW": 0, "powOutSumW": 10, "cmsBattSoc": 50}, select())
    controller.client_factory = lambda credentials: Client()
    controller.renew_credentials()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert attempts == [True]
    assert controller.connection == ConnectionState.LIVE
    assert not controller.credential_renewal_pending


@pytest.mark.parametrize("activation", ["click", "enter", "space"])
def test_failed_startup_key_request_can_be_retried_from_main_status(controller, qapp, monkeypatch, activation):
    controller.settings = replace(controller.settings, credential_source="1password", onepassword_access_ref="op://Test/Item/username", onepassword_secret_ref="op://Test/Item/password")
    attempts = []
    release = threading.Event()
    def load(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise OnePasswordError("onepassword_cancelled")
        assert release.wait(3)
        return Credentials("retry-access", "retry-secret")
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", load)
    class Client:
        def devices(self):
            return (Device("TEST", "Synthetic", True),)
        def read_quotas(self, serial, select_quotas, **kwargs):
            return normalize_partial({"powInSumW": 0, "powOutSumW": 0, "cmsBattSoc": 50}, select_quotas())
    controller.client_factory = lambda credentials: Client()
    controller.discover()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.can_retry_credentials and len(attempts) == 1
    controller.tick()
    qapp.processEvents()
    assert len(attempts) == 1
    page = MonitoringPage(controller, lambda: None)
    page.show()
    page.status.refresh()
    try:
        assert page.status.focusPolicy() == Qt.FocusPolicy.StrongFocus
        if activation == "click":
            page.status.click()
        else:
            QTest.keyClick(page.status, Qt.Key.Key_Return if activation == "enter" else Qt.Key.Key_Space)
        wait_until(qapp, lambda: len(attempts) == 2)
        page.status.click()
        assert len(attempts) == 2
        release.set()
        wait_until(qapp, lambda: "api" not in controller._active)
        assert controller.connection == ConnectionState.LIVE
        assert not controller.can_retry_credentials
    finally:
        release.set()
        page.close()


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_connection_sections_remain_readable_at_localized_scales(controller, qapp, monkeypatch, language, scale):
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1920, 1200))
    controller.settings = replace(controller.settings, language=language, scale=scale,
                                  onepassword_access_ref="op://Test/Item/username", onepassword_secret_ref="op://Test/Item/password")
    i18n.setup(language)
    window = Dashboard(controller, lambda: None)
    window.resize(min(1920, round(1184 * scale)), min(1200, round(900 * scale)))
    window.navigation.setCurrentRow(1)
    window.show()
    try:
        assert window.connection_tabs.count() == 3
        for index in range(3):
            window.connection_tabs.setCurrentIndex(index)
            if index == 0:
                window.device_profiles.support.set_expanded(True)
            elif index == 1:
                window.onepassword.source.setCurrentIndex(1)
                window.onepassword.save_section.set_expanded(True)
            qapp.processEvents()
            scroll = window.tabs.widget(1)
            target = window.device_profiles.support.header if index == 0 else window.onepassword.create_access if index == 1 else window.logging_mode
            scroll.ensureWidgetVisible(target)
            qapp.processEvents()
            for tab in range(3):
                rect = window.connection_tabs.tabBar().tabRect(tab)
                text = window.connection_tabs.tabText(tab)
                assert rect.width() >= window.connection_tabs.tabBar().fontMetrics().horizontalAdvance(text)
            assert window.device_profiles.detail.wordWrap()
            folder = Path("build/device-profile-preview") / f"{language}-{scale:g}"
            folder.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(folder / f"connection-{index}.png"))
        window.navigation.setCurrentRow(0)
        controller.settings = replace(controller.settings, credential_source="1password")
        controller.connection = ConnectionState.AUTH_ERROR
        controller.notice = "onepassword_access_error"
        controller.changed.emit()
        qapp.processEvents()
        status = window.monitoring.status
        assert status.width() >= status.sizeHint().width()
        assert status.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert window.grab().save(str(folder / "monitoring-retry.png"))
    finally:
        window.reject()

from dataclasses import replace
from datetime import datetime, timedelta
import json
import logging
import os
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget
import httpx
import pytest
from qt_helpers import wait_until
from qt_helpers import close_runtime

from ecoflow_monitor.adapters.ecoflow import Credentials, Device, EcoFlowClient
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.device_metadata import CapacitySampler, DeviceMetadataStore
from ecoflow_monitor.diagnostics import DailySizeFileHandler, DiagnosticLog
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.ui.confirmations import ConfirmationCoordinator, ConfirmationRequest


def wait(qapp, condition):
    wait_until(qapp, condition, timeout=4000)


def prompt(owner):
    dialog = QMessageBox(owner)
    dialog.setText("Synthetic permission")
    dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    return dialog


def test_confirmation_queue_waits_for_external_completion(qapp):
    owner = QWidget()
    queue = ConfirmationCoordinator(owner)
    completions = []
    queue.enqueue(ConfirmationRequest("first", prompt, completions.append))
    queue.enqueue(ConfirmationRequest("second", prompt, completions.append))
    queue.enqueue(ConfirmationRequest("second", prompt, completions.append))
    try:
        wait(qapp, lambda: queue.dialog is not None)
        assert queue.active.key == "first"
        assert queue.dialog.windowModality() == Qt.WindowModality.ApplicationModal
        assert queue.dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        queue.dialog.done(QMessageBox.StandardButton.Yes)
        qapp.processEvents()
        assert len(completions) == 1 and queue.dialog is None and queue.active.key == "first"
        completions[0]()
        wait(qapp, lambda: queue.dialog is not None)
        assert queue.active.key == "second"
        queue.dialog.done(QMessageBox.StandardButton.No)
        wait(qapp, lambda: queue.active is None)
        assert len(completions) == 1 and not queue.pending
    finally:
        queue.close()
        owner.close()
        owner.deleteLater()
        qapp.processEvents()


def test_external_provider_waits_behind_confirmation(qapp):
    owner = QWidget()
    queue = ConfirmationCoordinator(owner)
    release = []
    entered = threading.Event()
    cancelled = threading.Event()
    queue.enqueue(ConfirmationRequest("helper", None, release.append))
    wait(qapp, lambda: bool(release))
    thread = threading.Thread(target=lambda: queue.run_external(entered.set, cancelled))
    thread.start()
    try:
        wait(qapp, lambda: bool(queue.pending))
        assert not entered.is_set()
        release[0]()
        wait(qapp, entered.is_set)
        wait(qapp, lambda: queue.active is None)
    finally:
        cancelled.set()
        queue.close()
        thread.join(2)
        owner.close()
        owner.deleteLater()
        qapp.processEvents()


def test_queue_invalidates_changed_integration_and_closes(qapp):
    owner = QWidget()
    queue = ConfirmationCoordinator(owner)
    valid = [True]
    accepted, declined = [], []
    queue.enqueue(ConfirmationRequest("module", prompt, accepted.append, lambda: valid[0], lambda: declined.append(True)))
    wait(qapp, lambda: queue.dialog is not None)
    valid[0] = False
    queue.invalidate()
    wait(qapp, lambda: queue.active is None)
    assert declined == [True] and not accepted
    queue.close()
    assert not queue._watching
    owner.close()
    owner.deleteLater()
    qapp.processEvents()


def test_queue_replaces_obsolete_pending_request(qapp):
    owner = QWidget()
    queue = ConfirmationCoordinator(owner)
    release = []
    valid = [True]
    queue.enqueue(ConfirmationRequest("provider", None, release.append))
    wait(qapp, lambda: bool(release))
    queue.enqueue(ConfirmationRequest("module", prompt, lambda done: done(), lambda: valid[0]))
    valid[0] = False
    queue.enqueue(ConfirmationRequest("module", prompt, lambda done: done()))
    try:
        release[0]()
        wait(qapp, lambda: queue.dialog is not None)
        assert queue.active.key == "module"
        queue.dialog.done(QMessageBox.StandardButton.No)
    finally:
        queue.close()
        owner.deleteLater()
        qapp.processEvents()


@pytest.fixture
def runtime(tmp_path, qapp, session_cache):
    controller = Controller(SettingsStore(tmp_path / "settings.json"), credential_cache=session_cache)
    instance = Application(controller.store.path, native=False, controller=controller)
    yield instance
    close_runtime(instance, qapp)


def test_startup_decline_blocks_keys_helpers_and_allows_manual_discovery(runtime, qapp, monkeypatch):
    controller = runtime.controller
    controller.settings = replace(controller.settings, serial="TEST")
    controller.access_allowed = False
    runtime._automatic_helpers_allowed = False
    loaded = []
    monkeypatch.setattr(controller, Controller._read_credentials.__name__, lambda *args: loaded.append(True) or Credentials("test", "test"))
    class Client:
        def devices(self):
            return (Device("TEST", "My station", True),)
    controller.client_factory = lambda credentials: Client()
    runtime._startup_consent()
    wait(qapp, lambda: runtime.confirmations.dialog is not None)
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
    controller.tick()
    qapp.processEvents()
    assert not loaded and not controller._active and not runtime._automatic_helpers_allowed
    controller.discover()
    wait(qapp, lambda: "api" not in controller._active)
    assert loaded == [True] and controller.access_allowed


def test_device_name_survives_save_and_restart(runtime, qapp):
    controller = runtime.controller
    controller.settings = replace(controller.settings, serial="TEST")
    devices = (Device("TEST", "Kitchen battery", True),)
    controller.catalog = devices
    controller.metadata.remember(devices)
    controller.metadata.save()
    runtime.dashboard._devices(devices)
    runtime.dashboard.capacity.setValue(2100)
    runtime.dashboard.save()
    assert runtime.dashboard.devices.currentText() == "Kitchen battery"
    assert controller.settings.serial == "TEST"
    runtime.dashboard.refresh()
    runtime.widget.refresh()
    assert runtime.dashboard.device_label.text() == runtime.widget.title.text() == "Kitchen battery"
    restored = DeviceMetadataStore(controller.metadata.path)
    assert restored.name("TEST") == "Kitchen battery"


def test_discovery_uses_existing_session_without_panel_keys(runtime, qapp, monkeypatch):
    controller = runtime.controller
    controller.settings = replace(controller.settings, credential_source="1password", onepassword_access_ref="op://vault/item/access", onepassword_secret_ref="op://vault/item/secret")
    reads = []
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", lambda *args: reads.append(True) or Credentials("test", "test"))
    controller.credential_cache.get((controller.settings.onepassword_access_ref, controller.settings.onepassword_secret_ref),
                                    controller.settings.onepassword_cache_ttl_seconds, threading.Event(),
                                    lambda: reads.append(True) or Credentials("test", "test"))
    runtime.dashboard.load_draft()
    class Client:
        def devices(self):
            return (Device("TEST", "Shared session", True),)
    controller.client_factory = lambda credentials: Client()
    runtime.dashboard.discover()
    wait(qapp, lambda: "api" not in controller._active)
    assert reads == [True] and runtime.dashboard.devices.currentText() == "Shared session"


@pytest.mark.parametrize("charging", [False, True])
def test_capacity_uses_full_battery_and_persists_robust_samples(tmp_path, charging):
    store = DeviceMetadataStore(tmp_path / "device-metadata.json")
    sampler = store.sampler("TEST")
    for index in range(7):
        soc = 50 + index * (0.5 if charging else -0.5)
        fraction = (100 - soc if charging else soc) / 100
        values = {"input_power": 500 if charging else 0, "output_power": 0 if charging else 500,
                  "battery_level": soc, "charging_state": 2 if charging else 1,
                  "device_charge_minutes" if charging else "device_discharge_minutes": 2000 * fraction / 500 * 60,
                  "minimum_discharge_level": 20, "maximum_charge_level": 80}
        sampler.observe(values, 1800000000 + index * 60, index * 60, 90)
    assert sampler.estimate.watt_hours == pytest.approx(2000)
    assert sampler.estimate.count >= 5
    sampler.samples.append((900000, 1800001000))
    assert sampler.estimate.watt_hours == pytest.approx(2000)
    store.save()
    restored = DeviceMetadataStore(store.path)
    assert restored.sampler("TEST").estimate.watt_hours == pytest.approx(2000)
    assert restored.sampler("OTHER").estimate is None


def test_capacity_rejects_wrong_direction_unstable_and_cached_values():
    sampler = CapacitySampler()
    base = {"input_power": 0, "output_power": 500, "battery_level": 50, "charging_state": 1, "device_discharge_minutes": 120}
    assert not sampler.observe(base, 1800000000, 0, 90)
    assert not sampler.observe(base, 1800000060, 60, 90)
    assert not sampler.observe(dict(base, output_power=900, device_discharge_minutes=119), 1800000120, 120, 90)
    assert not sampler.observe(dict(base, charging_state=2), 1800000180, 180, 90)
    assert not sampler.observe({"battery_level": 50}, 1800000240, 240, 90)
    assert sampler.estimate is None


def test_one_shot_diagnostics_honor_off_and_mask_responses(tmp_path):
    path = tmp_path / "monitor.log"
    operations = []
    def handle(request):
        operations.append((request.method, request.url.path))
        return httpx.Response(200, json={"code": "0", "data": [] if request.url.path.endswith("/list") else {"unknownMetric": 7, "secretKey": "private-secret"}})
    with DiagnosticLog(path) as diagnostics:
        client = EcoFlowClient(Credentials("private-access", "private-secret"), httpx.MockTransport(handle))
        assert client.diagnose("PRIVATE-SERIAL", diagnostics.capture) == ("selected_parameters",)
        assert diagnostics.mode == "off"
    content = path.read_text(encoding="utf-8")
    assert "unknownMetric" in content and "missing_fields" in content
    assert "private-secret" not in content and "PRIVATE-SERIAL" not in content
    assert [method for method, endpoint in operations] == ["GET", "GET", "POST"]


def test_rotation_uses_day_utf8_bytes_and_preserves_unrelated_files(tmp_path):
    path = tmp_path / "monitor.log"
    now = [datetime(2026, 11, 13, 12)]
    sink = DailySizeFileHandler(path, 20, clock=lambda: now[0])
    sink.setFormatter(logging.Formatter("%(message)s"))
    def write(text):
        record = logging.LogRecord("test", logging.INFO, "", 0, text, (), None)
        if sink.shouldRollover(record):
            sink.doRollover()
        sink.stream.write(text + "\n")
        sink.flush()
    try:
        write("ї" * 5)
        write("ї" * 5)
        assert len(list(tmp_path.glob("monitor.*.log"))) == 1
        now[0] += timedelta(days=1)
        write("next day")
        assert len(list(tmp_path.glob("monitor.*.log"))) == 2
        unrelated = tmp_path / "unrelated.log"
        unrelated.write_text("keep", encoding="utf-8")
        for item in tmp_path.glob("monitor.*.log"):
            old = (now[0] - timedelta(days=15)).timestamp()
            os.utime(item, (old, old))
        sink.prune()
        assert not list(tmp_path.glob("monitor.*.log")) and unrelated.exists() and path.exists()
    finally:
        sink.close()


def test_two_integration_helpers_wait_for_uac_and_handshake(runtime, qapp, monkeypatch):
    first = replace(runtime.controller.settings.integrations[0], enabled=True)
    second = replace(first, id="second", label="Second integration")
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(first, second)))
    submitted = []
    monkeypatch.setattr(runtime.controller, Controller._submit.__name__, lambda *args: submitted.append(args))
    runtime._offer_elevation(first.id)
    runtime._offer_elevation(second.id)
    wait(qapp, lambda: runtime.confirmations.dialog is not None)
    assert first.label in runtime.confirmations.dialog.text()
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.Yes)
    qapp.processEvents()
    assert [kind for kind, operation in submitted] == ["elevate:" + first.id]
    assert runtime.confirmations.dialog is None
    runtime.controller.task_finished.emit("elevate:" + first.id)
    wait(qapp, lambda: runtime.confirmations.dialog is not None)
    assert second.label in runtime.confirmations.dialog.text()
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
    assert len(submitted) == 1


def test_startup_catalog_is_loaded_once_and_shared_with_telemetry(runtime, qapp, monkeypatch):
    controller = runtime.controller
    now = [1000.0]
    controller.clock = lambda: now[0]
    controller.settings = replace(controller.settings, serial="TEST")
    controller.access_allowed = False
    requests = []
    def handle(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "deviceName": "Fresh name", "online": 1}]})
        return httpx.Response(200, json={"code": "0", "data": {"powInSumW": 0, "powOutSumW": 500, "cmsBattSoc": 50}})
    monkeypatch.setattr(controller, Controller._read_credentials.__name__, lambda *args: Credentials("test", "test"))
    controller.client_factory = lambda credentials: EcoFlowClient(credentials, httpx.MockTransport(handle))
    controller.devices_ready.connect(runtime._startup_catalog_finished)
    runtime.open_dashboard()
    runtime._startup_consent()
    wait(qapp, lambda: runtime.confirmations.dialog is not None)
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.Yes)
    wait(qapp, lambda: "api" not in controller._active)
    now[0] += 31
    controller.tick()
    wait(qapp, lambda: "api" not in controller._active)
    assert requests.count("/iot-open/sign/device/list") == 1
    assert requests.count("/iot-open/sign/device/quota") == 2
    assert controller.device_name == "Fresh name"


def test_sampling_piggybacks_without_primary_background_requests(runtime, qapp):
    controller = runtime.controller
    controller.set_sensor_demand("dashboard", ())
    controller.set_sensor_demand("widget", {"ac_output"})
    polling = controller.polling
    first = polling.begin(0)
    assert "cmsDsgRemTime" in first
    from ecoflow_monitor.polling import TelemetryPatch
    polling.complete(TelemetryPatch(tuple(key for key in polling.in_flight), ()), 1800000000, 0)
    polling.verified = True
    assert not polling.begin(30)
    assert polling.begin(60)
    controller.set_sensor_demand("widget", ())
    assert not polling.required and not polling.supplemental


def test_rotation_total_limit_removes_only_oldest_archives(tmp_path):
    path = tmp_path / "monitor.log"
    now = datetime.now()
    archives = []
    for index in range(3):
        item = tmp_path / f"monitor.2026-11-13.{index + 1:04d}.log"
        with item.open("wb") as stream:
            stream.truncate(40 * 1024 * 1024)
        stamp = now.timestamp() - (3 - index) * 60
        os.utime(item, (stamp, stamp))
        archives.append(item)
    sink = DailySizeFileHandler(path, 3 * 1024 * 1024)
    try:
        sink.prune()
        assert not archives[0].exists() and all(item.exists() for item in archives[1:])
        assert path.exists()
    finally:
        sink.close()


def test_unsaved_credentials_do_not_replace_active_account_catalog(runtime, qapp):
    controller = runtime.controller
    original = (Device("ACTIVE", "Active station", True),)
    controller.catalog = original
    controller.metadata.remember(original)
    class DraftClient:
        def devices(self):
            return (Device("DRAFT", "Draft station", True),)
    controller.client_factory = lambda credentials: DraftClient()
    controller.discover(Credentials("draft-access", "draft-secret"))
    wait(qapp, lambda: "api" not in controller._active)
    assert controller.catalog == original
    assert controller.metadata.name("ACTIVE") == "Active station"
    assert controller.metadata.name("DRAFT") == "DRAFT"


def test_startup_provider_decline_is_not_retried_automatically(runtime, qapp, monkeypatch):
    from ecoflow_monitor.adapters.onepassword import OnePasswordError
    controller = runtime.controller
    controller.settings = replace(controller.settings, serial="TEST", credential_source="1password",
                                  onepassword_access_ref="op://vault/item/access", onepassword_secret_ref="op://vault/item/secret")
    reads = []
    def denied(*args):
        reads.append(True)
        raise OnePasswordError("onepassword_cancelled")
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", denied)
    runtime.open_dashboard()
    runtime._startup_consent()
    wait(qapp, lambda: runtime.confirmations.dialog is not None)
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.Yes)
    wait(qapp, lambda: "api" not in controller._active)
    controller.tick()
    qapp.processEvents()
    assert reads == [True] and not controller.access_allowed and "api" not in controller._active


def test_unsupported_device_does_not_retry_when_visibility_changes(runtime):
    controller = runtime.controller
    controller.connection = ConnectionState.UNSUPPORTED
    controller._next_api = float("inf")
    controller.set_sensor_demand("dashboard", ())
    controller.set_sensor_demand("widget", {"battery_level"})
    assert controller._next_api == float("inf")


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_startup_confirmation_fits_wrapped_message(runtime, qapp, language, scale):
    from pathlib import Path
    from PySide6.QtCore import QEvent, QSize
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QScrollArea, QSizeGrip
    from ecoflow_monitor import i18n
    i18n.setup(language)
    runtime.dashboard._scale = scale
    runtime.open_dashboard()
    message = i18n.t("startup_consent_description") + "\n\n" + "\n".join(
        "• " + i18n.t(key).format(name="MSI Afterburner") for key in ("startup_onepassword", "startup_device_list", "startup_helper"))
    dialog = runtime._permission_prompt(runtime.dashboard, "startup_consent_title", message)
    try:
        dialog.show()
        QTest.qWait(100)
        label = dialog.findChild(QLabel, "qt_msgbox_label")
        scroll = dialog.findChild(QScrollArea, "dialog_message_scroll")
        assert label.wordWrap()
        assert label.width() <= dialog.width()
        assert label.height() >= label.heightForWidth(label.width())
        assert dialog.findChild(QSizeGrip).isVisible()
        initial = dialog.size()
        for size in (dialog.minimumSize() + QSize(80, 120), initial):
            dialog.resize(size)
            QTest.qWait(20)
            resized = dialog.size()
            assert resized == size.boundedTo(dialog.maximumSize())
            qapp.sendEvent(dialog, QEvent(QEvent.Type.LayoutRequest))
            QTest.qWait(20)
            assert dialog.size() == resized
            assert label.wordWrap()
            assert label.width() <= scroll.viewport().width()
            assert label.height() >= label.heightForWidth(label.width())
            for button in dialog.buttons():
                assert dialog.rect().contains(button.geometry().translated(button.parentWidget().mapTo(dialog, button.parentWidget().rect().topLeft())))
        destination = Path("build/connection-fixes")
        destination.mkdir(parents=True, exist_ok=True)
        dialog.grab().save(str(destination / f"startup-{language}-{scale}.png"))
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.design
def test_long_confirmation_scrolls_on_small_work_area(runtime, qapp, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QScreen
    from PySide6.QtWidgets import QScrollArea
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 800, 600))
    runtime.dashboard._scale = 2
    dialog = runtime._permission_prompt(runtime.dashboard, "startup_consent_title", "\n".join(["Synthetic operation requiring confirmation"] * 40))
    try:
        dialog.show()
        qapp.processEvents()
        scroll = dialog.findChild(QScrollArea, "dialog_message_scroll")
        assert scroll is not None and scroll.verticalScrollBar().maximum() > 0
        assert dialog.height() <= 600 and dialog.width() <= 800
        assert dialog.screen().availableGeometry().contains(dialog.geometry())
        assert dialog.button(QMessageBox.StandardButton.No).isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_connection_dialogs_render_with_long_names(runtime, qapp, monkeypatch, language, scale):
    from pathlib import Path
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QScreen
    from ecoflow_monitor import i18n
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1920, 1200))
    controller = runtime.controller
    module = replace(controller.settings.integrations[0], enabled=True, label="Synthetic integration with a descriptive name")
    controller.settings = replace(controller.settings, serial="TEST", language=language, scale=scale, integrations=(module,),
                                  widget=replace(controller.settings.widget, enabled=True, scale=scale, show_device_name=True))
    controller.metadata.remember_name("TEST", "Домашня батарея / Home battery")
    controller.metadata.sampler("TEST").samples = [(2000 + offset, 1800000000 + offset) for offset in range(6)]
    i18n.setup(language)
    runtime.preferences()
    root = Path("build/connection-preview") / language / str(round(scale * 100))
    root.mkdir(parents=True, exist_ok=True)
    try:
        runtime.open_dashboard(1)
        runtime.dashboard.load_draft()
        qapp.processEvents()
        assert runtime.dashboard.grab().save(str(root / "connection.png"))
        runtime.dashboard.tabs.widget(1).ensureWidgetVisible(runtime.dashboard.capacity_status)
        qapp.processEvents()
        assert runtime.dashboard.grab().save(str(root / "connection-capacity.png"))
        assert runtime.widget.grab().save(str(root / "widget.png"))
        runtime.open_dashboard(0)
        assert runtime.dashboard.grab().save(str(root / "monitoring.png"))
        runtime._startup_consent()
        wait(qapp, lambda: runtime.confirmations.dialog is not None)
        dialog = runtime.confirmations.dialog
        assert dialog.size().width() <= 1920 and dialog.size().height() <= 1200
        assert dialog.grab().toImage().pixelColor(dialog.width() // 2, 4).alpha() == 255
        assert dialog.grab().save(str(root / "startup.png"))
        dialog.done(QMessageBox.StandardButton.No)
        qapp.processEvents()
        runtime._offer_elevation(module.id)
        wait(qapp, lambda: runtime.confirmations.dialog is not None)
        assert runtime.confirmations.dialog.grab().save(str(root / "helper.png"))
        runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
        qapp.processEvents()
        runtime._offer_diagnostics()
        wait(qapp, lambda: runtime.confirmations.dialog is not None)
        assert runtime.confirmations.dialog.grab().save(str(root / "diagnostics.png"))
        runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
    finally:
        i18n.setup("en")

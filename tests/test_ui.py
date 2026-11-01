import ast
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtTest import QTest
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import AfterburnerObservation, Availability, ConnectionState, Telemetry
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.ui.common import metric_text, status_tooltip
from ecoflow_monitor.ui.common import effective_scale
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui import window as window_module


class NoCredentials:
    def read(self, reference):
        raise AssertionError("UI tests must not access credentials")


@pytest.fixture
def runtime(tmp_path, qapp):
    controller = Controller(SettingsStore(tmp_path / "settings.json"), NoCredentials())
    instance = Application(controller.store.path, native=False, controller=controller)
    yield instance
    instance.stop()
    instance.window.deleteLater()
    instance.mini.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    i18n.setup("en")


def test_initial_view_hides_msi_and_displays_unknown_data(runtime):
    assert runtime.window.msi_section.isHidden()
    assert "—" in runtime.window.limits.text()
    assert metric_text(runtime.controller, "input_power") == "—"
    assert "No successful update" in status_tooltip(runtime.controller)


def test_settings_are_drafts_and_reset_does_not_apply(runtime, qapp):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    dialog.scale.setValue(150)
    dialog.mini_enabled.setChecked(True)
    assert runtime.controller.settings.scale == 1
    assert runtime.controller.settings.mini_enabled is False
    dialog.load_draft()
    assert dialog.scale.value() == 100 and dialog.mini_enabled.isChecked() is False
    dialog.reject()
    dialog.deleteLater()


def test_settings_save_merges_background_auto_change(runtime):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, msi_enabled=True, auto_enabled=True))
    dialog = SettingsWindow(controller, runtime.window)
    dialog.scale.setValue(150)
    controller.set_auto(False)
    dialog.save()
    assert controller.settings.scale == 1.5 and controller.settings.auto_enabled is False
    assert controller.store.load().scale == 1.5
    dialog.deleteLater()


def test_msi_enable_reveals_only_draft_controls_until_save(runtime):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    assert not dialog.tabs.isTabVisible(dialog.rules_tab_index)
    dialog.msi_enabled.setChecked(True)
    assert dialog.tabs.isTabVisible(dialog.rules_tab_index)
    assert runtime.controller.settings.msi_enabled is False
    dialog.save()
    assert runtime.controller.settings.msi_enabled is True
    assert not runtime.window.msi_section.isHidden()
    dialog.deleteLater()


def test_mini_metric_selection_and_live_language_change(runtime, qapp):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language="uk", mini_enabled=True, mini_metrics=("output_power", "battery_level")))
    assert tuple(runtime.mini.labels) == ("output_power", "battery_level")
    assert runtime.mini.isVisible()
    assert i18n.get_current_language() == "uk"
    assert runtime.window.settings_button.toolTip() != "Settings"


def test_flow_animation_stops_when_hidden_or_stale(runtime, qapp):
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 280, 80, time.time(), time.monotonic(), min_soc=20, max_soc=90)
    controller.connection = ConnectionState.LIVE
    runtime.window.show()
    runtime.window.refresh()
    qapp.processEvents()
    assert runtime.window.flow._timer.isActive()
    controller.connection = ConnectionState.STALE
    runtime.window.refresh()
    assert not runtime.window.flow._timer.isActive()
    assert metric_text(controller, "estimated_runtime") == "—"
    controller.connection = ConnectionState.LIVE
    runtime.window.refresh()
    runtime.window.hide()
    assert not runtime.window.flow._timer.isActive()


def test_requested_profile_is_not_shown_as_confirmed(runtime):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, msi_enabled=True))
    controller.observation = AfterburnerObservation(Availability.READY)
    controller.requested_slot = 3
    runtime.window.refresh()
    assert "Requested profile: 3" in runtime.window.profile_status.text()
    assert not any(button.isChecked() for button in runtime.window.profile_buttons)


def test_every_literal_translation_key_exists_in_english():
    package = Path(i18n.__file__).resolve().parent.parent
    catalog = json.loads((package / "i18n" / "en" / "strings.json").read_text(encoding="utf-8"))
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value in catalog, f"Unknown translation in {path.name}: {node.args[0].value}"


@pytest.mark.parametrize("scale", [0.5, 1, 2, 3])
def test_scale_applies_to_labels_and_preserves_mini_status_space(runtime, qapp, scale):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, scale=scale, mini_enabled=True, mini_scale=scale))
    runtime.window.show()
    qapp.processEvents()
    actual = runtime.window._scale
    assert actual <= effective_scale(runtime.window, scale)
    assert runtime.window.title.font().pixelSize() == round(17 * actual)
    assert runtime.mini.status.height() >= runtime.mini.status.fontMetrics().height()
    dialog = SettingsWindow(controller, runtime.window)
    dialog.show()
    qapp.processEvents()
    assert dialog.capacity.font().pixelSize() == round(13 * effective_scale(dialog, scale))
    dialog.close()
    dialog.deleteLater()


def test_scale_roundtrip_does_not_accumulate_window_height(runtime, qapp):
    runtime.window.resize(600, 700)
    for _ in range(3):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=0.5))
        qapp.processEvents()
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=1))
        qapp.processEvents()
    assert runtime.window.width() == 600
    assert runtime.window.height() == 700


def test_zooming_past_screen_then_back_preserves_logical_geometry(runtime, qapp):
    runtime.window.resize(600, 700)
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale))
        qapp.processEvents()
    assert runtime.window.width() == 600
    assert runtime.window.height() == 700


def test_saved_scaled_geometry_is_not_scaled_again_on_startup(tmp_path, qapp):
    store = SettingsStore(tmp_path / "scaled.json")
    from ecoflow_monitor.settings import AppSettings
    store.save(replace(AppSettings(), scale=1.5, monitor_geometry=(0, 0, 700, 800)))
    controller = Controller(store, NoCredentials())
    instance = Application(store.path, native=False, controller=controller)
    try:
        qapp.processEvents()
        assert instance.window.width() == 700 and instance.window.height() == 800
    finally:
        instance.stop()
        instance.window.deleteLater()
        instance.mini.deleteLater()


def test_first_msi_enable_fits_two_line_profile_controls(runtime, qapp):
    runtime.controller.apply_settings(replace(runtime.controller.settings, msi_enabled=True))
    runtime.window.show()
    qapp.processEvents()
    scroller = runtime.window.profile_scroll
    assert scroller.viewport().height() >= scroller.widget().minimumSizeHint().height()
    assert runtime.window.auto_toggle.text() == i18n.t("automatic_mode")


@pytest.mark.parametrize("available,enabled,alpha", [(True, True, 140), (True, False, 255), (False, True, 255)])
def test_monitor_background_rendering_includes_scroll_and_msi_containers(runtime, qapp, monkeypatch, available, enabled, alpha):
    monkeypatch.setattr("ecoflow_monitor.ui.window.acrylic", lambda target, requested: available and requested)
    runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=enabled, msi_enabled=True))
    window = runtime.window
    window.show()
    qapp.processEvents()
    image = window.grab().toImage()
    for widget, point in ((window.shell, QPoint(4, window.shell.height() // 2)), (window.flow, QPoint(2, 2)), (window.msi_section, QPoint(2, 2)), (window.profile_scroll.widget(), QPoint(2, 2))):
        position = widget.mapTo(window, point)
        assert image.pixelColor(position).alpha() == alpha, widget.objectName()
    assert image.pixelColor(0, 0).alpha() == 0
    title = window.title.grab().toImage()
    assert max(title.pixelColor(x, y).alpha() for x in range(title.width()) for y in range(title.height())) == 255
    dialog = SettingsWindow(runtime.controller, window)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.grab().toImage().pixelColor(4, 4).alpha() == 255
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_transparency_draft_save_reset_and_restart(runtime, qapp, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.window.acrylic", lambda target, enabled: enabled)
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        dialog.transparency.setChecked(False)
        assert runtime.controller.settings.transparency is True
        dialog.load_draft()
        assert dialog.transparency.isChecked()
        dialog.transparency.setChecked(False)
        dialog.save()
        assert runtime.controller.store.load().transparency is False
        restarted_controller = Controller(runtime.controller.store, NoCredentials())
        restarted = window_module.MonitorWindow(restarted_controller, lambda: None)
        try:
            restarted.show()
            qapp.processEvents()
            assert restarted.grab().toImage().pixelColor(4, restarted.height() // 2).alpha() == 255
        finally:
            restarted._quitting = True
            restarted.close()
            restarted_controller.stop()
            restarted.deleteLater()
        runtime.window.show()
        runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=True))
        qapp.processEvents()
        assert runtime.window.grab().toImage().pixelColor(4, runtime.window.height() // 2).alpha() == 140
    finally:
        dialog.deleteLater()


def wait_for_ui(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


@pytest.mark.parametrize("visible", [False, True])
def test_acrylic_warmup_preserves_startup_visibility(runtime, monkeypatch, visible):
    monkeypatch.setattr("ecoflow_monitor.ui.window.sys", SimpleNamespace(platform="win32"))
    runtime.window._desired_visible = visible
    runtime.window.startup()
    wait_for_ui(lambda: not runtime.window._warming)
    assert runtime.window.isVisible() == visible
    for _ in range(3):
        runtime.window.request_visibility(True)
        assert runtime.window.isVisible()
        runtime.window.request_visibility(False)
        assert not runtime.window.isVisible()


def test_visibility_requests_during_both_warmup_phases_use_latest_request(runtime, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.window.sys", SimpleNamespace(platform="win32"))
    window = runtime.window
    window._reshow_timer.setInterval(100)
    window.startup()
    window.request_visibility(False)
    wait_for_ui(lambda: window._reshow_timer.isActive())
    window.request_visibility(True)
    assert window.isHidden()
    window.request_visibility(False)
    wait_for_ui(lambda: not window._warming)
    assert window.isHidden()
    assert runtime.controller.store.load().monitor_visible is False


@pytest.mark.parametrize("reshow", [False, True])
def test_shutdown_cancels_pending_acrylic_warmup(runtime, monkeypatch, reshow):
    monkeypatch.setattr("ecoflow_monitor.ui.window.sys", SimpleNamespace(platform="win32"))
    window = runtime.window
    window.startup()
    if reshow:
        wait_for_ui(lambda: window._reshow_timer.isActive())
    runtime.stop()
    QTest.qWait(250)
    assert window.isHidden()
    assert not window._warmup_timer.isActive()
    assert not window._reshow_timer.isActive()
    assert not window._backdrop_timer.isActive()


def test_show_and_native_handle_change_restore_backdrop(runtime, qapp, monkeypatch):
    available = False
    monkeypatch.setattr("ecoflow_monitor.ui.window.acrylic", lambda target, enabled: available and enabled)
    window = runtime.window
    window.show()
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == 255
    window.hide()
    available = True
    window.show()
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == 140
    available = False
    QCoreApplication.sendEvent(window, QEvent(QEvent.Type.WinIdChange))
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == 255

from dataclasses import replace
import sys

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, METRICS, WidgetPreferences
from ecoflow_monitor.ui.adaptive_widget import WIDGET
from ecoflow_monitor.widget_layout import Rectangle, details_placement, widget_layout


@pytest.fixture
def runtime(tmp_path, qapp, monkeypatch):
    failures = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, traceback: failures.append(error))
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    instance.stop()
    instance.dashboard.deleteLater()
    instance.widget.deleteLater()
    instance.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    i18n.setup("en")
    assert not failures, failures


@pytest.mark.parametrize("size,mode", [((240, 56), "minimal"), ((320, 88), "small"), ((360, 320), "large"), ((900, 88), "large"), ((639, 100), "small")])
def test_layout_selects_responsive_modes(size, mode):
    assert widget_layout(WidgetPreferences(), *size, 6, 6, WIDGET).mode == mode


def test_layout_respects_hierarchy_expanded_only_and_overflow():
    preferences = WidgetPreferences(small_metrics=("battery_level",), large_metrics=("battery_level", "input_power"), expanded_metrics=("output_power",))
    small = widget_layout(preferences, 320, 88, 1, 1, WIDGET)
    assert small.visible == ("battery_level",)
    assert small.details == ("output_power",)
    large = widget_layout(preferences, 900, 88, 1, 1, WIDGET)
    assert large.visible == ("battery_level",)
    assert large.details == ("input_power", "output_power")
    expanded = widget_layout(preferences, 900, 88, 6, 1, WIDGET)
    assert expanded.visible == ("battery_level", "input_power")
    assert expanded.details == ("output_power",)


def test_minimal_honors_small_selection_and_keeps_other_metrics_in_details():
    preferences = replace(WidgetPreferences(), small_metrics=("input_power", "estimated_runtime"))
    result = widget_layout(preferences, 240, 56, 3, 1, WIDGET)
    assert result.visible == ("input_power",)
    assert result.details == ("estimated_runtime",)


@pytest.mark.parametrize("dock,core,direction", [
    ("bottom", Rectangle(500, 744, 240, 56), "top"),
    ("top", Rectangle(500, 0, 240, 56), "bottom"),
    ("left", Rectangle(0, 300, 240, 56), "right"),
    ("right", Rectangle(960, 300, 240, 56), "left"),
    ("", Rectangle(500, 740, 240, 56), "top"),
])
def test_details_open_inward_without_moving_the_core(dock, core, direction):
    original = core
    actual, panel = details_placement(core, Rectangle(0, 0, 1200, 800), (320, 320), dock)
    assert actual == direction
    assert core == original
    assert 0 <= panel.x and panel.right <= 1200 and 0 <= panel.y and panel.bottom <= 800
    assert panel.right <= core.x or panel.x >= core.right or panel.bottom <= core.y or panel.y >= core.bottom


def test_migration_always_uses_main_and_retains_legacy_data():
    old = replace(AppSettings(), main_scale=1.25, background_opacity=0.37, monitor_geometry=(30, 40, 500, 350), mini_enabled=True, mini_scale=2, mini_position=(700, 800), mini_hidden_metrics=("estimated_runtime",)).to_dict()
    old["version"] = 3
    old.pop("widget")
    migrated = AppSettings.from_dict(old)
    assert migrated.widget.enabled
    assert migrated.widget.geometry == (30, 40, 500, 350)
    assert migrated.widget.scale == 1.25 and migrated.widget.opacity == 0.37
    assert migrated.widget.small_metrics == migrated.widget.large_metrics == METRICS[:3]
    assert migrated.widget.expanded_metrics == ("estimated_runtime",)
    assert migrated.mini_position == (700, 800) and migrated.mini_scale == 2
    assert AppSettings.from_dict(migrated.to_dict()) == migrated
    assert migrated.to_dict()["version"] == 4


@pytest.mark.parametrize("changes", [
    {"small_metrics": ("input_power",), "large_metrics": ()},
    {"expanded_metrics": ("battery_level",)},
    {"large_metrics": (), "small_metrics": ()},
    {"metric_order": METRICS[:-1]}, {"opacity": True}, {"dock": "invalid"},
])
def test_invalid_widget_settings_rejected(changes):
    with pytest.raises(ValueError):
        replace(WidgetPreferences(), **changes).validate()


def test_application_owns_one_overlay_and_hotkeys_are_aliases(runtime):
    assert runtime.widget is runtime.main is runtime.mini
    assert not runtime.widget.isVisible()
    runtime.hotkey("toggle_mini")
    assert runtime.widget.isVisible()
    runtime.hotkey("toggle_main")
    assert not runtime.widget.isVisible()
    assert runtime.main_action is runtime.mini_action


def test_visibility_flags_are_cumulative_and_details_only_is_exclusive(runtime):
    editor = runtime.dashboard.metrics
    row = editor.rows["estimated_runtime"]
    row.controls["small"].setChecked(True)
    assert row.controls["large"].isChecked() and not row.controls["large"].isEnabled()
    draft = runtime.dashboard.gather(False).widget
    assert "estimated_runtime" in draft.small_metrics and "estimated_runtime" in draft.large_metrics
    row.controls["expanded"].setChecked(True)
    draft = runtime.dashboard.gather(False).widget
    assert "estimated_runtime" in draft.expanded_metrics
    assert "estimated_runtime" not in draft.large_metrics and "estimated_runtime" not in draft.small_metrics


def test_runtime_changes_preserve_other_staged_widget_fields(runtime):
    dashboard = runtime.dashboard
    dashboard.main_enabled.setChecked(True)
    dashboard.main_alpha.setValue(40)
    dashboard.overlay_fields["main"]["position_locked"].setChecked(True)
    dashboard._draft_changed()
    assert runtime.widget.isVisible()
    assert runtime.widget.overlay.change(pinned=False)
    assert runtime.controller.ui_settings.widget.opacity == 0.4
    assert runtime.controller.ui_settings.widget.overlay.position_locked
    runtime.widget.close_button.click()
    dashboard.save()
    saved = runtime.controller.store.load().widget
    assert not saved.enabled and not saved.overlay.pinned
    assert saved.overlay.position_locked and saved.opacity == 0.4


def test_click_through_requires_recovery_and_explicit_recovery_updates_draft(runtime):
    assert not runtime.widget.overlay.change(click_through=True)
    assert not runtime.controller.settings.widget.overlay.click_through
    runtime.dashboard.overlay_fields["main"]["click_through"].setChecked(True)
    runtime.dashboard._draft_changed()
    runtime.recover_widgets()
    assert not runtime.controller.ui_settings.widget.overlay.click_through
    assert not runtime.dashboard.gather(False).widget.overlay.click_through


def test_details_toggle_does_not_change_base_geometry_or_button_positions(runtime, qapp):
    runtime.controller.update_widget(enabled=True, small_metrics=("battery_level",), large_metrics=("battery_level",), expanded_metrics=("input_power",))
    widget = runtime.widget
    geometry = widget.geometry()
    controls = [button.geometry() for button in (widget.overlay.pin, widget.reveal_button, widget.overlay.menu_button, widget.close_button)]
    widget.reveal_button.setChecked(True)
    qapp.processEvents()
    assert widget.details.isVisible()
    assert widget.geometry() == geometry
    assert controls == [button.geometry() for button in (widget.overlay.pin, widget.reveal_button, widget.overlay.menu_button, widget.close_button)]
    widget.controller.changed.emit()
    assert widget.details.isVisible()
    QTest.keyClick(widget.details, Qt.Key.Key_Escape)
    assert not widget.details.isVisible()


def test_controls_follow_pointer_and_keyboard_without_reserving_space(runtime, qapp, monkeypatch):
    runtime.controller.update_widget(enabled=True)
    widget = runtime.widget
    monkeypatch.setattr(widget, QWidget.underMouse.__name__, lambda: False)
    monkeypatch.setattr(widget.details, QWidget.underMouse.__name__, lambda: False)
    widget.activateWindow()
    qapp.processEvents()
    widget.clearFocus()
    focus = qapp.focusWidget()
    if focus is not None and focus.window() is widget:
        focus.clearFocus()
    QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    qapp.processEvents()
    geometry = widget.geometry()
    readings = {key: reading.geometry() for key, reading in widget.readings.items()}
    assert not widget.toolbar.isVisible()
    point = QPointF(20, 20)
    QCoreApplication.sendEvent(widget, QEnterEvent(point, point, point))
    qapp.processEvents()
    assert widget.toolbar.isVisible()
    QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    qapp.processEvents()
    assert not widget.toolbar.isVisible()
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    assert widget.toolbar.isVisible()
    assert widget.geometry() == geometry
    assert readings == {key: reading.geometry() for key, reading in widget.readings.items()}
    QTest.keyClick(widget, Qt.Key.Key_Tab)
    assert widget.overlay.pin.hasFocus()


@pytest.mark.parametrize("state,warning", [(ConnectionState.LIVE, False), (ConnectionState.STALE, True), (ConnectionState.DEVICE_OFFLINE, True), (ConnectionState.API_ERROR, True), (ConnectionState.CONNECTING, True)])
def test_hidden_status_keeps_nonlive_warning_and_stops_energy(runtime, state, warning):
    controller = runtime.controller
    controller.clock = lambda: 1000
    controller.telemetry = Telemetry(640, 185, 72, 1700000000, 1000)
    controller.connection = state
    controller.update_widget(enabled=True)
    assert runtime.widget.status.isHidden()
    assert runtime.widget.status_indicator.isVisible() == warning
    if warning:
        assert runtime.widget.status_indicator.accessibleName() == runtime.widget.status_indicator.toolTip()
        assert runtime.motion_driver.state.mode == "unknown"


def test_detail_panel_retains_open_state_across_pin_and_refresh(runtime):
    runtime.controller.update_widget(enabled=True, large_metrics=METRICS[:3], expanded_metrics=METRICS[3:])
    widget = runtime.widget
    widget.reveal_button.setChecked(True)
    assert widget.details.isVisible()
    assert widget.overlay.change(pinned=False)
    assert widget.details.isVisible() and widget.reveal_button.isChecked()
    assert not widget.details.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    runtime.controller.changed.emit()
    assert widget.details.isVisible()
    runtime.controller.update_widget(enabled=False)
    assert not widget.details.isVisible() and not widget.reveal_button.isChecked()


@pytest.mark.design
@pytest.mark.parametrize("size", [(240, 56), (320, 88), (360, 320), (760, 88)])
@pytest.mark.parametrize("language", ["en", "uk"])
def test_visible_readings_fit_and_have_inner_padding(runtime, qapp, size, language):
    runtime.controller.apply_settings(replace(runtime.controller.settings, language=language))
    runtime.controller.update_widget(enabled=True, opacity=0)
    widget = runtime.widget
    widget.resize(*size)
    qapp.processEvents()
    if size == (240, 56) or size == (320, 88):
        assert widget.composition.visible == METRICS[:3]
    assert widget.composition.visible
    for key in widget.composition.visible:
        reading = widget.readings[key]
        assert widget.rect().contains(reading.geometry())
        assert reading.rect().contains(reading.value.geometry())
        assert reading.box.contentsMargins().left() > 0
        assert reading.value.outlined


@pytest.mark.design
def test_large_to_wide_releases_hero_minimum_without_clipping(runtime, qapp):
    runtime.controller.update_widget(enabled=True)
    widget = runtime.widget
    widget.resize(512, 350)
    qapp.processEvents()
    widget.resize(760, 88)
    qapp.processEvents()
    assert widget.composition.visible
    for key in widget.composition.visible:
        reading = widget.readings[key]
        assert widget.rect().contains(reading.geometry())
        assert reading.rect().contains(reading.value.geometry())

from dataclasses import replace
from types import SimpleNamespace
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QLabel, QWidget
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.elevation import ElevatedSession
from ecoflow_monitor.adapters.integrations import IntegrationObservation, TransitionResult
from ecoflow_monitor.adapters.hotkeys import HotkeyManager
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings
from ecoflow_monitor.ui.readings import MainReadings
from ecoflow_monitor.ui.common import metric_text
from ecoflow_monitor.ui.integrations import ProfileDialog
from ecoflow_monitor.ui.scrollbar import QuietScrollBar
from ecoflow_monitor.ui.settings_window import SettingsWindow


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    instance.stop()
    instance.window.deleteLater()
    instance.mini.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("labels,hover", [(True, False), (False, False), (True, True), (False, True)])
def test_display_preferences_roundtrip_and_legacy_defaults(labels, hover):
    settings = replace(AppSettings(), monitor_labels_visible=labels, monitor_values_on_hover=hover)
    assert AppSettings.from_dict(settings.to_dict()) == settings
    data = settings.to_dict()
    del data["appearance"]["monitor_labels_visible"]
    del data["appearance"]["monitor_values_on_hover"]
    legacy = AppSettings.from_dict(data)
    assert legacy.monitor_labels_visible and not legacy.monitor_values_on_hover


@pytest.mark.parametrize("key", ["monitor_labels_visible", "monitor_values_on_hover"])
@pytest.mark.parametrize("invalid", [0, 1, None, "false"])
def test_display_preferences_reject_non_booleans(key, invalid):
    data = AppSettings().to_dict()
    data["appearance"][key] = invalid
    with pytest.raises(ValueError):
        AppSettings.from_dict(data)
    with pytest.raises(ValueError):
        replace(AppSettings(), **{key: invalid}).validate()


def test_display_drafts_reset_save_cancel_and_merge(runtime):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        assert dialog.appearance_sections["appearance_general"].header.isChecked()
        section = dialog.appearance_sections["appearance_monitor"]
        section.set_expanded(True)
        dialog.monitor_labels_visible.setChecked(False)
        dialog.monitor_values_on_hover.setChecked(True)
        assert runtime.controller.settings.monitor_labels_visible
        dialog.load_draft()
        assert section.header.isChecked()
        assert dialog.monitor_labels_visible.isChecked()
        assert not dialog.monitor_values_on_hover.isChecked()
        dialog.monitor_labels_visible.setChecked(False)
        dialog.monitor_values_on_hover.setChecked(True)
        runtime.controller.update_preferences(mini_enabled=True)
        draft = dialog.gather()
        assert draft.mini_enabled and not draft.monitor_labels_visible and draft.monitor_values_on_hover
        dialog.reject()
        assert runtime.controller.settings.monitor_labels_visible
        dialog.save()
        stored = runtime.controller.store.load()
        assert stored.mini_enabled and not stored.monitor_labels_visible and stored.monitor_values_on_hover
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("size", [(640, 620), (900, 700), (320, 310)])
@pytest.mark.parametrize("fresh", [True, False])
def test_hidden_metric_text_and_hover_use_paint_transform(runtime, qapp, size, fresh):
    controller = runtime.controller
    controller.telemetry = Telemetry(120, 80, 65, time.time(), time.monotonic())
    controller.connection = ConnectionState.LIVE if fresh else ConnectionState.STALE
    controller.apply_settings(replace(controller.settings, animations=False, monitor_labels_visible=False, monitor_values_on_hover=True))
    flow = MainReadings(controller)
    flow.configure(1)
    flow.resize(*size)
    flow.show()
    qapp.processEvents()
    try:
        for key, reading in flow.readings.items():
            assert reading.label.isHidden()
            reading.setFocus()
            qapp.processEvents()
            assert reading.value.isVisible()
            assert reading.toolTip()
            assert reading.value.text() == metric_text(controller, key)
    finally:
        flow.close()
        flow.deleteLater()


def test_charge_limit_values_reveal_independently_without_resize(runtime, qapp):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, manual_min=10, manual_max=90, monitor_labels_visible=False, monitor_values_on_hover=True))
    runtime.window.show()
    qapp.processEvents()
    minimum, maximum = runtime.window.limits.readouts
    QTest.mouseMove(runtime.window.title, QPoint(4, 4))
    size = runtime.window.size()
    assert minimum.display_text() == maximum.display_text() == ""
    QTest.mouseMove(minimum, minimum.rect().center())
    assert minimum.display_text() == "10 %" and maximum.display_text() == ""
    QTest.mouseMove(maximum, maximum.rect().center())
    assert maximum.display_text() == "90 %" and minimum.display_text() == ""
    assert runtime.window.size() == size
    assert "Source: Manual" in minimum.toolTip()


def test_notifications_include_all_modules_and_preserve_shortcut_conflicts(runtime):
    controller = runtime.controller
    first = replace(controller.settings.integrations[0], enabled=True)
    second = replace(first, id="second", label="Second integration")
    controller.apply_settings(replace(controller.settings, integrations=(first, second)))
    first_state = controller.integration_states[first.id]
    first_state.observation = IntegrationObservation("integration_ready")
    second_state = controller.integration_states[second.id]
    second_state.observation = IntegrationObservation("integration_ready")
    second_state.requested_profile = second.profiles[0].id
    second_state.result = TransitionResult(False, 2, "activation", 3, "integration_command_failed", exit_code=7, winerror=740)
    runtime.window.hotkey_notice = "Shortcut conflict"
    controller.notice = "integration_helper_ready"
    runtime.window.refresh()
    icon = runtime.window.notifications
    text = icon.toolTip()
    assert icon.has_warning and "Shortcut conflict" in text
    assert first.label in text and second.label in text
    assert "Exit code: 7" in text and "Windows error: 740" in text
    assert "Requested profile" in text and "Current profile" not in text
    runtime.window.integration_selector.setCurrentIndex(1)
    assert icon.toolTip() == text
    assert not runtime.window.findChildren(QLabel, "profile_status")
    second_state.result = TransitionResult(True, 1, exit_code=0)
    runtime.window.hotkey_notice = ""
    controller.notice = ""
    runtime.window.refresh()
    assert not icon.has_warning and "Command completed" in icon.toolTip()
    assert "Exit code: 7" not in icon.toolTip()
    controller.apply_settings(replace(controller.settings, language="uk"))
    assert icon.toolTip() != text and icon.accessibleName() == i18n.t("notifications")
    controller.apply_settings(replace(controller.settings, integrations=()))
    assert icon.isHidden()


class WindowShows(QObject):
    def __init__(self):
        super().__init__()
        self.windows = []

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.windows.append(watched)
        return False


def test_settings_rebuilds_never_show_temporary_top_level_widgets(runtime, qapp):
    module = replace(runtime.controller.settings.integrations[0], enabled=True)
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(module,)))
    observer = WindowShows()
    qapp.installEventFilter(observer)
    dialog = None
    try:
        dialog = SettingsWindow(runtime.controller, runtime.window)
        assert not observer.windows
        dialog.show()
        dialog.tabs.setCurrentIndex(1)
        dialog.integrations_editor.add()
        dialog.integrations_editor.enabled.setChecked(True)
        dialog.integrations_editor.system_rules.add_rule()
        dialog.integrations_editor.select(0)
        dialog.hotkey_sections[module.id].set_expanded(True)
        dialog._refresh_hotkeys()
        dialog.load_draft()
        qapp.processEvents()
        assert observer.windows == [dialog]
        assert all(section.parentWidget() is dialog.hotkey_body for section in dialog.hotkey_sections.values())
    finally:
        qapp.removeEventFilter(observer)
        if dialog is not None:
            dialog.reject()
            dialog.deleteLater()


def test_scrollbars_grow_without_changing_viewport_and_drag_normally(runtime, qapp):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        dialog.show()
        dialog.tabs.setCurrentIndex(2)
        for section in dialog.appearance_sections.values():
            section.set_expanded(True)
        dialog.resize(600, 400)
        qapp.processEvents()
        for area in dialog.findChildren(QAbstractScrollArea):
            assert isinstance(area.verticalScrollBar(), QuietScrollBar)
            assert isinstance(area.horizontalScrollBar(), QuietScrollBar)
        area = dialog.tabs.currentWidget()
        bar = area.verticalScrollBar()
        assert bar.maximum() > bar.minimum() and bar.isVisible()
        QTest.mouseMove(dialog, QPoint(5, 5))
        viewport_size = area.viewport().size()
        narrow = bar.thumb_rect().width()
        QTest.mouseMove(bar, bar.thumb_rect().center().toPoint())
        assert bar.thumb_rect().width() > narrow
        assert area.viewport().size() == viewport_size
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=bar.thumb_rect().center().toPoint())
        QTest.mouseMove(bar, QPoint(bar.width() // 2, bar.height() - 5))
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=QPoint(bar.width() // 2, bar.height() - 5))
        assert bar.value() > bar.minimum()
        assert area.viewport().size() == viewport_size
        profile = ProfileDialog(runtime.controller.settings.integrations[0], parent=dialog)
        try:
            assert all(isinstance(area.verticalScrollBar(), QuietScrollBar) for area in profile.findChildren(QAbstractScrollArea))
        finally:
            profile.deleteLater()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_resize_overlays_cover_outer_edges_and_do_not_reserve_layout_space(runtime: Application, qapp, monkeypatch):
    window = runtime.window
    window.show()
    qapp.processEvents()
    controls = window.resize_controls
    layout_size = window.minimumSizeHint()
    for point in (QPoint(0, 0), QPoint(window.width() - 1, 0), QPoint(0, window.height() - 1), QPoint(window.width() - 1, window.height() - 1), QPoint(0, window.height() // 2), QPoint(window.width() - 1, window.height() // 2), QPoint(window.width() // 2, 0), QPoint(window.width() // 2, window.height() - 1)):
        assert window.childAt(point) in controls.edges
    requests = []
    monkeypatch.setattr(QWindow, "startSystemResize", lambda self, edges: requests.append(edges) or True)
    for edge in controls.edges:
        QTest.mouseClick(edge, Qt.MouseButton.LeftButton)
    assert requests == [edge.edges for edge in controls.edges]
    QTest.mouseMove(window.flow, window.flow.rect().center())
    qapp.processEvents()
    assert controls.grip.isVisible()
    QTest.mousePress(controls.grip, Qt.MouseButton.LeftButton)
    assert requests[-1] == Qt.Edge.BottomEdge | Qt.Edge.RightEdge
    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
    assert controls.grip.isHidden()
    QTest.mouseRelease(controls.grip, Qt.MouseButton.LeftButton)
    assert window._shell_layout.indexOf(controls.grip) == -1
    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
    assert controls.grip.isHidden()
    assert window.minimumSizeHint() == layout_size
    window.resize(window.width() + 40, window.height() + 20)
    assert controls.edges[-1].geometry().bottomRight() == window.rect().bottomRight()
    window._save_geometry()
    assert window.controller.settings.monitor_geometry[2:] == (window.width(), window.height())


def test_tray_checks_follow_visibility_actions_and_hotkeys(runtime, qapp):
    assert not runtime.monitor_action.isChecked()
    runtime.monitor_action.trigger()
    assert runtime.window.isVisible() and runtime.monitor_action.isChecked()
    runtime.monitor_action.trigger()
    assert not runtime.window.isVisible() and not runtime.monitor_action.isChecked()
    runtime.hotkey("toggle_monitor")
    assert runtime.monitor_action.isChecked()
    runtime.window.close_button.click()
    assert not runtime.monitor_action.isChecked()
    runtime.mini_action.trigger()
    assert runtime.mini.isVisible() and runtime.mini_action.isChecked()
    runtime.hotkey("toggle_mini")
    assert not runtime.mini.isVisible() and not runtime.mini_action.isChecked()
    runtime.controller.apply_settings(replace(runtime.controller.settings, mini_enabled=True))
    assert runtime.mini_action.isChecked()


def test_helper_check_tracks_real_session_liveness_and_invalidates_edits(runtime: Application, monkeypatch):
    module = replace(runtime.controller.settings.integrations[0], enabled=True)
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(module,)))
    runtime.native = True
    monkeypatch.setattr(HotkeyManager, "configure", lambda self, *args: [])
    runtime.preferences()
    session = ElevatedSession(module, "example.exe")
    session.connection = SimpleNamespace(close=lambda: None)
    session.handle = 123
    running = [True]
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.process_running", lambda handle: running[0])
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.close_handle", lambda handle: None)
    runtime.controller.integration_adapter.sessions[module.id] = session
    runtime._sync_menu()
    action = runtime._helper_actions[module.id]
    assert action.isChecked() and not action.isEnabled()
    runtime._offer_elevation(module.id)
    running[0] = False
    runtime._sync_menu()
    assert not action.isChecked() and action.isEnabled()
    assert not runtime.controller.integration_adapter.helper_ready(replace(module, executable="changed.exe"))
    session.close()
    assert not session.ready

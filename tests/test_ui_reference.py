import ctypes
from ctypes import wintypes
from dataclasses import replace
import time
from types import SimpleNamespace

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea
import pytest
from qt_helpers import close_runtime

from ecoflow_monitor.adapters import hotkeys
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import HotkeyBinding
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor
from ecoflow_monitor.ui.settings_window import SettingsWindow


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    close_runtime(instance, qapp)


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("msi", [False, True])
@pytest.mark.design
def test_repeated_scaling_keeps_all_monitor_content_visible(runtime, qapp, language, msi):
    controller = runtime.controller
    window = runtime.dashboard
    controller.apply_settings(replace(controller.settings, language=language, integrations=(replace(controller.settings.integrations[0], enabled=msi),)))
    window.show()
    for scale in (0.5, 1, 2, 3, 0.5, 3, 1):
        controller.apply_settings(replace(controller.settings, scale=scale))
        window.resize(640, 480)
        qapp.processEvents()
        assert window.screen().availableGeometry().contains(window.geometry())
        assert controller.settings.scale == scale
        assert window.monitoring.readings["battery_level"].value.font().pixelSize() == round(36 * scale)
        page = window.tabs.widget(0)
        assert page.widget().height() >= page.widget().minimumSizeHint().height()
        page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
        assert window.monitoring.msi_section.isHidden() is (not msi)


@pytest.mark.design
def test_tray_menu_does_not_inherit_widget_scale_or_theme(runtime, qapp):
    menu = runtime.menu
    menu.ensurePolished()
    initial_font = menu.font()
    initial_size = menu.sizeHint()
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, widget=replace(runtime.controller.settings.widget, scale=scale), scale=scale))
        qapp.processEvents()
        assert menu.parentWidget() is None
        assert menu.font() == initial_font
        assert menu.sizeHint() == initial_size
        assert "#151B20" in menu.styleSheet() and "#74E3BD" in menu.styleSheet()


@pytest.mark.parametrize("state,color", [
    (ConnectionState.UNCONFIGURED, "#A8B7C2"),
    (ConnectionState.CONNECTING, "#FFC884"),
    (ConnectionState.LIVE, "#74E3BD"),
    (ConnectionState.STALE, "#FFC884"),
    (ConnectionState.DEVICE_OFFLINE, "#A8B7C2"),
    (ConnectionState.AUTH_ERROR, "#FF9B9B"),
])
def test_status_icon_and_tooltip_are_shared_by_both_widgets(runtime, qapp, state, color):
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 100, 80, time.time(), time.monotonic())
    controller.connection = state
    runtime.window.refresh()
    runtime.mini.refresh()
    for label in (runtime.window.monitoring.status, runtime.mini.status):
        assert runtime.controller.connection.value != label.text()
        assert label.text() and label.text() != "●"
        assert "\n" in label.toolTip()
        assert label.accessibleName() == label.toolTip()
    assert runtime.window.monitoring.isAncestorOf(runtime.window.monitoring.status)


@pytest.mark.design
@pytest.mark.parametrize("state,color", [(ConnectionState.LIVE, "#74E3BD"), (ConnectionState.STALE, "#FFC884"), (ConnectionState.AUTH_ERROR, "#FF9B9B")])
def test_status_colors_follow_semantic_roles(runtime, state, color):
    runtime.controller.telemetry = Telemetry(0, 100, 80, time.time(), time.monotonic())
    runtime.controller.connection = state
    runtime.window.refresh()
    runtime.mini.refresh()
    assert color in runtime.window.monitoring.status.styleSheet()
    assert color in runtime.mini.status.styleSheet()


def test_hotkey_editor_keeps_reference_capture_selection_and_clear(qapp):
    editor = HotkeyComboEditor("ctrl+alt+num5")
    try:
        editor.start_recording()
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_7, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier, "7")
        editor.keyPressEvent(event)
        assert editor.combo() == "ctrl+num7"
        editor.assign("ctrl+f24")
        assert editor.combo() == "ctrl+f24"
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        assert editor.combo() == ""
    finally:
        editor.deleteLater()


def test_hotkey_rows_are_drafts_and_follow_msi_visibility(runtime):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        field, editor, enabled = dialog.hotkey_rows["profile:afterburner:profile_3"]
        assert field.isHidden()
        dialog.integrations_editor.enabled.setChecked(True)
        field, editor, enabled = dialog.hotkey_rows["profile:afterburner:profile_3"]
        assert not field.isHidden()
        editor.set_combo("ctrl+shift+num3")
        enabled.setChecked(True)
        assert not any(binding.enabled for binding in runtime.controller.settings.hotkeys if binding.action == "profile:afterburner:profile_3")
        dialog.save()
        binding = next(item for item in runtime.controller.store.load().hotkeys if item.action == "profile:afterburner:profile_3")
        assert binding == HotkeyBinding("profile:afterburner:profile_3", "ctrl+shift+num3", True)
    finally:
        dialog.deleteLater()


class NativeFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def test_manager_replaces_bindings_delivers_both_event_types_and_reports_conflicts(qapp, monkeypatch):
    registered = {}
    def register(hwnd, identifier, modifiers, key):
        if key == 0x70:
            return False
        registered[identifier] = (modifiers, key)
        return True
    def unregister(hwnd, identifier):
        registered.pop(identifier)
        return True
    user = SimpleNamespace(RegisterHotKey=NativeFunction(register), UnregisterHotKey=NativeFunction(unregister))
    monkeypatch.setattr(hotkeys.ctypes, "WinDLL", lambda name, **kwargs: user)
    manager = hotkeys.HotkeyManager()
    received = []
    manager.triggered.connect(received.append)
    bindings = (HotkeyBinding("toggle_monitor", "ctrl+alt+num5", True), HotkeyBinding("profile_1", "ctrl+num1", True), HotkeyBinding("toggle_mini", "ctrl+f1", True))
    try:
        assert manager.configure(bindings[:1], {"toggle_monitor"}) == ()
        previous = dict(registered)
        assert manager.configure(bindings, {"toggle_monitor", "toggle_mini"}) == ("toggle_mini",)
        assert registered == previous
        assert len(registered) == 1
        assert next(iter(registered.values())) == (3 | 0x4000, 0x65)
        message = wintypes.MSG()
        message.message = 0x0312
        message.wParam = next(iter(registered))
        for event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            assert manager.nativeEventFilter(event_type, ctypes.addressof(message)) == (True, 0)
        assert received == ["toggle_monitor", "toggle_monitor"]
        old_ids = set(registered)
        assert manager.configure(bindings[:2], {binding.action for binding in bindings[:2]}) == ()
        assert len(registered) == 2 and old_ids.issubset(registered)
    finally:
        manager.stop()
    assert registered == {}


def test_geometry_preference_updates_do_not_reload_global_hotkeys(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr("ecoflow_monitor.adapters.hotkeys.HotkeyManager.configure", lambda *args: calls.append(args) or ())
    runtime.native = True
    runtime.preferences()
    runtime.controller.update_preferences(widget=replace(runtime.controller.settings.widget, geometry=(10, 10, 600, 700)))
    assert len(calls) == 1
    runtime.controller.update_preferences(integrations=(replace(runtime.controller.settings.integrations[0], enabled=True),))
    assert len(calls) == 2


@pytest.mark.design
def test_screen_and_dpi_changes_restore_desired_scale_without_clipping(runtime, qapp):
    window = runtime.dashboard
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=3))
    window.resize(640, 480)
    window.show()
    qapp.processEvents()
    assert window.width() <= window.screen().availableGeometry().width()
    assert window._scale == 3
    assert window.sidebar.isHidden()
    assert window.navigation_button.isVisible()
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=1))
    window.resize(1184, 900)
    qapp.processEvents()
    assert not window.sidebar.isHidden()
    assert window._scale == 1


def test_rapid_scale_changes_settle_at_final_scale(runtime, qapp):
    for scale in (3, 0.5, 2, 1, 3, 0.5):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale))
    qapp.processEvents()
    assert runtime.window._scale == 0.5
    assert runtime.window.minimumWidth() >= 180
    for reading in runtime.window.monitoring.readings.values():
        assert reading.width() >= reading.minimumSizeHint().width()


@pytest.mark.parametrize("dock", ["", "left", "right", "top", "bottom"])
@pytest.mark.design
def test_all_mini_metrics_fit_work_area_and_small_scale_labels(runtime, qapp, dock):
    from ecoflow_monitor.settings import METRICS
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, widget=replace(runtime.controller.settings.widget, enabled=True, small_metrics=METRICS, large_metrics=METRICS, scale=scale, dock=dock)))
        qapp.processEvents()
        mini = runtime.mini
        assert mini.screen().availableGeometry().contains(mini.geometry())
        for label in mini.labels.values():
            assert label.height() >= label.fontMetrics().height()

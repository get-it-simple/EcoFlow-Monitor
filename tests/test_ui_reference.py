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

from ecoflow_monitor.adapters import hotkeys
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import HotkeyBinding, SettingsStore
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui.window import MonitorWindow


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    instance.stop()
    instance.window.deleteLater()
    instance.mini.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("msi", [False, True])
def test_repeated_scaling_keeps_all_monitor_content_visible(runtime, qapp, language, msi):
    controller = runtime.controller
    window = runtime.window
    controller.apply_settings(replace(controller.settings, language=language, msi_enabled=msi))
    window.show()
    for scale in (0.5, 1, 2, 3, 0.5, 3, 1):
        controller.apply_settings(replace(controller.settings, scale=scale))
        qapp.processEvents()
        window.resize(window.minimumSize())
        qapp.processEvents()
        assert window.screen().availableGeometry().contains(window.geometry())
        assert window.flow.width() >= window.flow.minimumWidth()
        assert window.flow.height() >= window.flow.minimumHeight()
        assert window.rect().contains(window.flow.geometry().translated(window.shell.pos()))
        assert window.status.isVisible()
        for scroller in window.findChildren(QScrollArea):
            assert not scroller.verticalScrollBar().isVisible()
            assert not scroller.horizontalScrollBar().isVisible()
            if scroller.isVisible():
                assert scroller.viewport().height() >= scroller.widget().minimumSizeHint().height()
                assert scroller.viewport().width() >= scroller.widget().minimumSizeHint().width()
        assert controller.settings.scale == scale


def test_tray_menu_does_not_inherit_widget_scale_or_theme(runtime, qapp):
    menu = runtime.menu
    menu.ensurePolished()
    initial_font = menu.font()
    initial_size = menu.sizeHint()
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale, mini_scale=scale))
        qapp.processEvents()
        assert menu.parentWidget() is None
        assert menu.font() == initial_font
        assert menu.sizeHint() == initial_size
        assert menu.styleSheet() == ""


@pytest.mark.parametrize("state,color", [
    (ConnectionState.UNCONFIGURED, "#ffc16c"),
    (ConnectionState.CONNECTING, "#ffc16c"),
    (ConnectionState.LIVE, "#50e9a2"),
    (ConnectionState.STALE, "#ff6060"),
    (ConnectionState.DEVICE_OFFLINE, "#ff6060"),
    (ConnectionState.AUTH_ERROR, "#ff6060"),
])
def test_status_icon_and_tooltip_are_shared_by_both_widgets(runtime, qapp, state, color):
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 100, 80, time.time(), time.monotonic())
    controller.connection = state
    runtime.window.refresh()
    runtime.mini.refresh()
    for label in (runtime.window.status, runtime.mini.status):
        assert label.text() == "●"
        assert color in label.styleSheet()
        assert "\n" in label.toolTip()
        assert label.accessibleName() == label.toolTip()
    assert runtime.window.status.parentWidget() is runtime.window.flow


def test_hotkey_editor_keeps_reference_capture_selection_and_clear(qapp):
    editor = HotkeyComboEditor("ctrl+alt+num5")
    try:
        assert editor.combo() == "ctrl+alt+num5"
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_7, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier, "7")
        editor.keyPressEvent(event)
        assert editor.combo() == "ctrl+num7"
        key_box = editor._select_boxes[-1]
        key_box.setCurrentIndex(key_box.findData("f24"))
        assert editor.combo() == "ctrl+f24"
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        assert editor.combo() == ""
    finally:
        editor.deleteLater()


def test_hotkey_rows_are_drafts_and_follow_msi_visibility(runtime):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        field, editor, enabled = dialog.hotkey_rows["profile_3"]
        assert field.isHidden()
        dialog.msi_enabled.setChecked(True)
        assert not field.isHidden()
        editor.set_combo("ctrl+shift+num3")
        enabled.setChecked(True)
        assert not runtime.controller.settings.hotkeys[5].enabled
        dialog.save()
        binding = next(item for item in runtime.controller.store.load().hotkeys if item.action == "profile_3")
        assert binding == HotkeyBinding("profile_3", "ctrl+shift+num3", True)
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
    monkeypatch.setattr(hotkeys.ctypes, "WinDLL", lambda name: user)
    manager = hotkeys.HotkeyManager()
    received = []
    manager.triggered.connect(received.append)
    bindings = (HotkeyBinding("toggle_monitor", "ctrl+alt+num5", True), HotkeyBinding("profile_1", "ctrl+num1", True), HotkeyBinding("toggle_mini", "ctrl+f1", True))
    try:
        assert manager.configure(bindings, False) == ("toggle_mini",)
        assert len(registered) == 1
        assert next(iter(registered.values())) == (3 | 0x4000, 0x65)
        message = wintypes.MSG()
        message.message = 0x0312
        message.wParam = next(iter(registered))
        for event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            assert manager.nativeEventFilter(event_type, ctypes.addressof(message)) == (True, 0)
        assert received == ["toggle_monitor", "toggle_monitor"]
        old_ids = set(registered)
        assert manager.configure(bindings[:2], True) == ()
        assert len(registered) == 2 and not old_ids.intersection(registered)
    finally:
        manager.stop()
    assert registered == {}


def test_geometry_preference_updates_do_not_reload_global_hotkeys(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr("ecoflow_monitor.adapters.hotkeys.HotkeyManager.configure", lambda *args: calls.append(args) or ())
    runtime.native = True
    runtime.preferences()
    runtime.controller.update_preferences(monitor_geometry=(10, 10, 600, 700))
    assert len(calls) == 1
    runtime.controller.update_preferences(msi_enabled=True)
    assert len(calls) == 2


def test_screen_and_dpi_changes_restore_desired_scale_without_clipping(runtime, qapp):
    area = QRect(0, 0, 640, 480)
    ratio = 2.0
    class ScreenConstrainedMonitor(MonitorWindow):
        def screen(self):
            return SimpleNamespace(availableGeometry=lambda: area)

        def devicePixelRatioF(self):
            return ratio

    window = ScreenConstrainedMonitor(runtime.controller, lambda: None)
    try:
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=3, msi_enabled=True))
        qapp.processEvents()
        assert window.width() <= 640 and window.height() <= 480
        assert window._scale <= 1.5
        assert runtime.controller.settings.scale == 3
        small_scale = window._scale
        area = QRect(0, 0, 7680, 4320)
        ratio = 1.0
        window.apply_appearance()
        assert window._scale == 3 and window._scale > small_scale
        assert window.flow.width() >= window.flow.minimumWidth()
        assert window.flow.height() >= window.flow.minimumHeight()
    finally:
        window._quitting = True
        window.close()
        window.deleteLater()


def test_rapid_scale_changes_settle_at_final_scale(runtime, qapp):
    for scale in (3, 0.5, 2, 1, 3, 0.5):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale))
    qapp.processEvents()
    assert runtime.window._scale == 0.5
    assert runtime.window.flow.minimumSize().width() == 210


@pytest.mark.parametrize("dock", ["", "left", "right", "top", "bottom"])
def test_all_mini_metrics_fit_work_area_and_small_scale_labels(runtime, qapp, dock):
    from ecoflow_monitor.settings import METRICS
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, mini_enabled=True, mini_metrics=METRICS, mini_scale=scale, mini_dock=dock))
        qapp.processEvents()
        mini = runtime.mini
        assert mini.screen().availableGeometry().contains(mini.geometry())
        for label in mini.labels.values():
            assert label.height() >= label.fontMetrics().height()

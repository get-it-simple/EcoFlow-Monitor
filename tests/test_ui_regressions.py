from dataclasses import replace
from types import SimpleNamespace
import ctypes
import time

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QDialog, QLineEdit, QMessageBox, QStyle, QStyleOptionSlider, QSystemTrayIcon, QToolTip

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.hotkeys import HotkeyError, HotkeyIssue, HotkeyManager, validate_bindings
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import HotkeyBinding
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor, KeyPicker, hotkey_error_text
from ecoflow_monitor.ui.integrations import CommandDialog


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    instance.stop()
    instance.dashboard.deleteLater()
    instance.mini.deleteLater()
    qapp.processEvents()
    i18n.setup("en")


def test_mini_close_overrides_preview_and_preserves_other_draft_edits(runtime, qapp):
    dashboard = runtime.dashboard
    dashboard.capacity.setValue(3000)
    dashboard.mini_enabled.setChecked(True)
    dashboard._draft_changed()
    assert runtime.mini.isVisible()
    assert not runtime.controller.settings.mini_enabled
    QTest.mouseClick(runtime.mini.close_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert not runtime.mini.isVisible()
    assert not dashboard.mini_enabled.isChecked()
    assert dashboard.capacity.value() == 3000
    dashboard._draft_changed()
    assert not runtime.mini.isVisible()
    dashboard.save()
    stored = runtime.controller.store.load()
    assert not stored.mini_enabled and stored.capacity_wh == 3000


def test_mini_runtime_actions_merge_with_preview_without_reopening(runtime, qapp):
    runtime.controller.update_preferences(mini_enabled=True)
    dashboard = runtime.dashboard
    dashboard.mini_alpha.setValue(40)
    dashboard._draft_changed()
    QTest.mouseClick(runtime.mini.overlay.pin, Qt.MouseButton.LeftButton)
    assert not runtime.controller.settings.mini_overlay.pinned
    assert not dashboard.overlay_fields["mini"]["pinned"].isChecked()
    assert runtime.controller.ui_settings.mini_transparency == pytest.approx(0.6)
    runtime.hotkey("toggle_mini")
    qapp.processEvents()
    assert not runtime.mini.isVisible() and not dashboard.mini_enabled.isChecked()
    runtime.mini_action.trigger()
    qapp.processEvents()
    assert runtime.mini.isVisible() and dashboard.mini_enabled.isChecked()
    dashboard.reject()
    assert runtime.mini.isVisible()
    assert not runtime.mini.overlay.pin.isChecked()
    assert runtime.controller.ui_settings.mini_transparency == pytest.approx(0.18)


def test_unrelated_save_and_mini_controls_ignore_existing_native_shortcut_failure(runtime, monkeypatch):
    runtime.native = True
    def failed_registration(manager, bindings, actions):
        pytest.fail("Unchanged shortcuts must not be registered for this operation")
    monkeypatch.setattr(HotkeyManager, "configure", failed_registration)
    runtime.controller.update_preferences(mini_enabled=True)
    assert runtime.mini.overlay.change(pinned=False)
    assert runtime.mini.overlay.change(position_locked=True)
    runtime.mini.close_button.click()
    assert not runtime.mini.isVisible()
    runtime.dashboard.capacity.setValue(3000)
    runtime.dashboard.save()
    assert runtime.controller.store.load().capacity_wh == 3000
    assert not runtime.controller.settings.mini_overlay.pinned
    assert runtime.controller.settings.mini_overlay.position_locked


def test_recovery_registration_failure_retains_pointer_control_and_working_bindings(runtime, monkeypatch):
    runtime.native = True
    monkeypatch.setattr(QSystemTrayIcon, QSystemTrayIcon.isVisible.__name__, lambda self: True)
    runtime.hotkeys._bindings = {77: "toggle_monitor"}
    def fail(manager, bindings, actions):
        manager.issues = (HotkeyIssue("recover_widgets", "ctrl+alt+w", "hotkey_registration_failed", winerror=1409),)
        return ("recover_widgets",)
    monkeypatch.setattr(HotkeyManager, "configure", fail)
    assert not runtime.mini.overlay.change(click_through=True)
    assert not runtime.controller.settings.mini_overlay.click_through
    assert not runtime.mini.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert runtime.hotkeys._bindings == {77: "toggle_monitor"}
    assert "ctrl+alt+w" in runtime._hotkey_notice and "1409" in runtime._hotkey_notice
    runtime.hotkeys._bindings.clear()


def test_duplicate_error_identifies_both_actions_and_preserves_inputs(runtime):
    dashboard = runtime.dashboard
    editor = dashboard.hotkey_rows["toggle_mini"][1]
    dashboard.hotkey_rows["toggle_mini"][2].setChecked(True)
    editor.set_combo("alt+ctrl+num5")
    dashboard.capacity.setValue(3000)
    dashboard._draft_changed()
    assert not dashboard.save_button.isEnabled()
    assert editor.combo() == "alt+ctrl+num5"
    assert dashboard.capacity.value() == 3000
    assert i18n.t("toggle_monitor") in dashboard.error_label.text()
    assert i18n.t("toggle_mini") in dashboard.error_label.text()
    for action in ("toggle_monitor", "toggle_mini"):
        assert dashboard.hotkey_rows[action][1].property("state") == "conflict"
    editor.set_combo("ctrl+alt+5")
    dashboard._draft_changed()
    assert dashboard.save_button.isEnabled()
    assert dashboard.hotkey_rows["toggle_monitor"][1].property("state") == "assigned"
    assert not editor.toolTip()


def test_registration_error_names_action_and_keeps_draft_after_save_failure(runtime, monkeypatch):
    runtime.native = True
    original = runtime.controller.settings
    editor = runtime.dashboard.hotkey_rows["toggle_monitor"][1]
    editor.set_combo("ctrl+f24")
    def fail(manager, bindings, actions):
        manager.issues = (HotkeyIssue("toggle_monitor", "ctrl+f24", "hotkey_registration_failed", winerror=1409),)
        return ("toggle_monitor",)
    monkeypatch.setattr(HotkeyManager, "configure", fail)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, message: messages.append(message))
    runtime.dashboard.save()
    assert runtime.controller.settings == original
    assert editor.combo() == "ctrl+f24"
    assert len(messages) == 1
    assert "Windows" in messages[0] and "ctrl+f24" in messages[0] and "1409" in messages[0]
    assert i18n.t("toggle_monitor") in messages[0]
    assert messages[0] == runtime.dashboard.error_label.text()
    assert editor.property("state") == "conflict"


def test_invalid_binding_has_distinct_diagnostic():
    with pytest.raises(HotkeyError) as caught:
        validate_bindings((HotkeyBinding("toggle_mini", "ctrl+ctrl", True),))
    assert caught.value.issues[0].reason == "invalid_hotkey"
    assert i18n.t("invalid_hotkey") in hotkey_error_text(caught.value, ())


class NativeFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def test_native_registration_error_records_windows_code_and_rolls_back(qapp, monkeypatch):
    from ecoflow_monitor.adapters import hotkeys
    registered = {}
    def register(hwnd, identifier, modifiers, key):
        if key == 0x87:
            ctypes.set_last_error(1409)
            return False
        registered[identifier] = (modifiers, key)
        return True
    def unregister(hwnd, identifier):
        registered.pop(identifier)
        return True
    user = SimpleNamespace(RegisterHotKey=NativeFunction(register), UnregisterHotKey=NativeFunction(unregister))
    monkeypatch.setattr(hotkeys.ctypes, "WinDLL", lambda name, **kwargs: user)
    manager = HotkeyManager()
    first = HotkeyBinding("toggle_monitor", "ctrl+num5", True)
    try:
        assert manager.configure((first,), {first.action}) == ()
        old = dict(registered)
        pending = (first, HotkeyBinding("toggle_mini", "ctrl+f7", True), HotkeyBinding("recover_widgets", "ctrl+f24", True))
        assert manager.configure(pending, {binding.action for binding in pending}) == ("recover_widgets",)
        assert registered == old
        assert manager.issues == (HotkeyIssue("recover_widgets", "ctrl+f24", "hotkey_registration_failed", winerror=1409),)
    finally:
        manager.stop()
        ctypes.set_last_error(0)


@pytest.mark.parametrize("accept", [True, False])
def test_command_double_click_has_no_inline_editor_after_dialog(runtime, qapp, monkeypatch, accept):
    editor = runtime.dashboard.integrations_editor
    runtime.open_dashboard(3)
    editor.body.setCurrentIndex(1)
    qapp.processEvents()
    table = editor.mapping
    table.selectRow(0)
    original = table.item(0, 1).text()
    calls = []
    def finish(dialog):
        calls.append(dialog)
        if accept:
            dialog.label.setText("Updated command")
            dialog.save()
            return QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(CommandDialog, QDialog.exec.__name__, finish)
    item = table.item(0, 1)
    point = table.visualItemRect(item).center()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qapp.processEvents()
    assert len(calls) == 1
    assert table.item(0, 1).text() == ("Updated command" if accept else original)
    assert table.state() != QAbstractItemView.State.EditingState
    assert not table.findChildren(QLineEdit)


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_key_picker_is_bounded_searchable_and_keyboard_accessible(runtime, qapp, scale):
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale, language="uk"))
    editor = runtime.dashboard.hotkey_rows["toggle_monitor"][1]
    runtime.open_dashboard(4)
    qapp.processEvents()
    errors = []
    def choose():
        popup = QApplication.activePopupWidget()
        try:
            assert isinstance(popup, KeyPicker)
            area = popup.screen().availableGeometry()
            assert area.contains(popup.geometry())
            assert popup.height() <= round(area.height() * 0.6)
            assert popup.keys.verticalScrollBar().maximum() > 0
            assert popup.search.isVisible() and all(box.isVisible() for box in popup.modifiers.values())
            popup.modifiers["alt"].setChecked(False)
            popup.search.setText("F24")
            QTest.keyClick(popup.search, Qt.Key.Key_Return)
        except BaseException as error:
            errors.append(error)
            if popup is not None:
                popup.reject()
    QTimer.singleShot(0, choose)
    editor.choose_key()
    qapp.processEvents()
    assert not errors
    assert editor.combo() == "ctrl+f24"
    assert editor.hasFocus()


def test_toolbar_changes_preserve_other_overlay_drafts_and_recovery_cancels_pending_passthrough(runtime):
    dashboard = runtime.dashboard
    dashboard.overlay_fields["mini"]["position_locked"].setChecked(True)
    dashboard.overlay_fields["mini"]["click_through"].setChecked(True)
    dashboard._draft_changed()
    assert runtime.mini.overlay.change(pinned=False)
    assert dashboard.overlay_fields["mini"]["position_locked"].isChecked()
    assert runtime.controller.ui_settings.mini_overlay.position_locked
    runtime.recover_widgets()
    assert not dashboard.overlay_fields["mini"]["click_through"].isChecked()
    assert not runtime.controller.ui_settings.mini_overlay.click_through
    assert dashboard.overlay_fields["mini"]["position_locked"].isChecked()
    dashboard.save()
    assert runtime.controller.store.load().mini_overlay.position_locked
    assert not runtime.controller.store.load().mini_overlay.click_through


def test_key_picker_escape_keeps_previous_combo(qapp):
    editor = HotkeyComboEditor("ctrl+num5")
    try:
        editor.show()
        QTimer.singleShot(0, lambda: QTest.keyClick(QApplication.activePopupWidget(), Qt.Key.Key_Escape))
        editor.choose_key()
        assert editor.combo() == "ctrl+num5"
    finally:
        editor.deleteLater()


@pytest.mark.parametrize("application", [False, True])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_rule_charge_fields_show_synchronized_slider_and_number(runtime, qapp, application, scale):
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale, language="uk"))
    runtime.open_dashboard(3)
    editor = runtime.dashboard.integrations_editor
    editor.body.setCurrentIndex(3)
    rules = editor.application_rules if application else editor.system_rules
    from ecoflow_monitor.integrations import IntegrationRule
    rules.append(IntegrationRule("synthetic", rules.profiles[0].id, executable="example.exe" if application else ""))
    qapp.processEvents()
    column = 3 if application else 2
    for index, value in enumerate((20, 90)):
        field = rules.table.cellWidget(rules.table.rowCount() - 1, column + index)
        assert field.slider.isVisible()
        assert field.rect().contains(field.slider.geometry())
        assert field.rect().contains(field.spin.geometry())
        field.toggle.setChecked(True)
        field.slider.setValue(value)
        option = QStyleOptionSlider()
        field.slider.initStyleOption(option)
        handle = field.slider.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, field.slider)
        assert field.slider.rect().contains(handle)
        assert field.spin.value() == field.value() == value
        field.spin.setValue(value + 1)
        assert field.slider.value() == value + 1
        field.toggle.setChecked(False)
        assert not field.spin.isEnabled() and not field.slider.isEnabled()
        field.toggle.setChecked(True)
        assert field.value() == value + 1


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
@pytest.mark.parametrize("integration_warning", [False, True])
def test_dashboard_minimum_fits_work_area_and_monitoring_has_no_horizontal_clip(runtime, qapp, scale, integration_warning):
    module = replace(runtime.controller.settings.integrations[0], enabled=integration_warning)
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale, language="uk", integrations=(module,)))
    if integration_warning:
        runtime.controller.telemetry = Telemetry(640, 185, 72, time.time(), time.monotonic(), 20, 90)
        runtime.controller.connection = ConnectionState.LIVE
        runtime.controller.changed.emit()
    dashboard = runtime.dashboard
    dashboard.show()
    area = dashboard.screen().availableGeometry()
    dashboard.resize(360, 320)
    qapp.processEvents()
    assert dashboard.width() >= min(round(800 * dashboard._scale), area.width())
    assert dashboard.height() >= min(round(600 * dashboard._scale), area.height())
    assert dashboard.width() <= area.width() and dashboard.height() <= area.height()
    assert dashboard.tabs.widget(0).horizontalScrollBar().maximum() == 0
    if integration_warning:
        assert dashboard.monitoring.notifications.isVisible()


def test_saved_small_dashboard_geometry_is_raised_to_new_minimum(tmp_path, qapp):
    from ecoflow_monitor.settings import AppSettings, SettingsStore
    path = tmp_path / "settings.json"
    SettingsStore(path).save(replace(AppSettings(), dashboard_geometry=(20, 20, 360, 320)))
    runtime = Application(path, native=False)
    try:
        assert runtime.dashboard.width() >= 800 and runtime.dashboard.height() >= 600
    finally:
        runtime.stop()
        runtime.dashboard.deleteLater()
        runtime.mini.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("opacity", [0, 0.82, 1])
def test_mini_tooltip_and_menu_do_not_inherit_button_size(runtime, qapp, opacity):
    runtime.controller.update_preferences(mini_enabled=True, mini_transparency=1 - opacity)
    button = runtime.mini.overlay.menu_button
    QToolTip.showText(button.mapToGlobal(QPoint(0, button.height())), "Full tooltip text for widget controls", button)
    qapp.processEvents()
    tip = next(widget for widget in QApplication.topLevelWidgets() if widget.objectName() == "qtooltip_label")
    assert tip.width() > button.width() * 2
    assert tip.height() >= tip.fontMetrics().height()
    QToolTip.hideText()
    menu = runtime.mini.overlay.menu
    menu.popup(button.mapToGlobal(QPoint(0, button.height())))
    qapp.processEvents()
    try:
        assert menu.width() > button.width() * 2
        for item in menu.actions():
            assert menu.actionGeometry(item).height() >= menu.fontMetrics().height()
    finally:
        menu.close()

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
import pytest

from ecoflow_monitor.adapters import reference_effects
from ecoflow_monitor.application import Application
from ecoflow_monitor.integrations import IntegrationRule
from ecoflow_monitor.ui.common import app_icon
from ecoflow_monitor.ui.integrations import ProfileDialog
from ecoflow_monitor.ui.percent_editor import PercentEditor
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui.theme import accent_text


@pytest.mark.parametrize("minimum,maximum,decimals", [(0, 100, 0), (50, 300, 0), (0, 100, 2)])
def test_percent_editor_synchronizes_numeric_slider_and_keyboard(qapp, minimum, maximum, decimals):
    editor = PercentEditor(minimum, maximum, decimals)
    try:
        for value in (minimum, maximum, (minimum + maximum) // 2):
            editor.slider.setValue(value)
            assert editor.value() == value
            editor.spin.setValue(maximum - value + minimum)
            assert editor.slider.value() == round(editor.value())
        editor.slider.setValue(minimum)
        QTest.keyClick(editor.slider, Qt.Key.Key_Right)
        assert editor.value() == minimum + 1
        editor.spin.lineEdit().selectAll()
        QTest.keyClicks(editor.spin.lineEdit(), "42.75" if decimals else str(maximum))
        QTest.keyClick(editor.spin, Qt.Key.Key_Return)
        assert editor.value() == (42.75 if decimals else maximum)
        if decimals:
            assert editor.slider.value() == 43
            assert editor.spin.value() == 42.75
        editor.setValue(maximum + 50)
        assert editor.value() == maximum
        editor.setValue(minimum - 50)
        assert editor.value() == minimum
    finally:
        editor.deleteLater()


def test_optional_percent_retains_value_when_unset(qapp):
    editor = PercentEditor(optional=True, default=100)
    try:
        assert editor.value() == -1
        assert not editor.slider.isEnabled() and not editor.spin.isEnabled()
        changes = QSignalSpy(editor.valueChanged)
        editor.setValue(100)
        assert changes.count() == 1
        assert editor.value() == editor.slider.value() == 100
        editor.slider.setValue(65)
        editor.toggle.setChecked(False)
        assert editor.value() == -1
        editor.toggle.setChecked(True)
        assert editor.value() == 65
        editor.setValue(-1)
        assert editor.value() == -1 and editor.spin.value() == 65
    finally:
        editor.deleteLater()


def test_accent_refresh_preserves_open_dialog_drafts(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(reference_effects, "get_accent_color", lambda: "#1259be")
    runtime = Application(tmp_path / "settings.json", native=False)
    dialog = SettingsWindow(runtime.controller, runtime.window)
    module = runtime.controller.settings.integrations[0]
    profile = ProfileDialog(module, module.profiles[0], dialog)
    try:
        dialog.tabs.setCurrentIndex(2)
        dialog.appearance_tabs.setCurrentIndex(0)
        dialog.scale.slider.setValue(175)
        editor = dialog.hotkey_rows["toggle_monitor"][1]
        editor.set_combo("ctrl+f12")
        profile.label.setText("Unsaved profile")
        before = dialog.gather()
        spy = QSignalSpy(runtime.accent.changed)
        monkeypatch.setattr(reference_effects, "get_accent_color", lambda: "#edb820")
        message = wintypes.MSG()
        message.message = 0x0320
        assert runtime.accent.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)) == (False, 0)
        qapp.processEvents()
        assert spy.count() == 1
        for widget in (dialog, profile, runtime.window):
            assert "#74E3BD" in widget.styleSheet()
            assert "#edb820" not in widget.styleSheet()
        assert dialog.gather() == before
        assert dialog.tabs.currentIndex() == 2 and dialog.appearance_tabs.currentIndex() == 0
        assert profile.label.text() == "Unsaved profile"
        assert editor.combo() == "ctrl+f12"
        runtime.accent.refresh()
        assert spy.count() == 1
        dialog.show()
        qapp.processEvents()
        assert dialog.grab().toImage().pixelColor(4, 4).name() == "#151b20"
        runtime.stop()
        monkeypatch.setattr(reference_effects, "get_accent_color", lambda: "#aa33dd")
        qapp.paletteChanged.emit(qapp.palette())
        qapp.processEvents()
        assert spy.count() == 1
    finally:
        runtime.stop()
        profile.deleteLater()
        dialog.deleteLater()
        runtime.window.deleteLater()
        runtime.mini.deleteLater()
        qapp.processEvents()


def test_percent_sliders_save_reset_cancel_and_rules(tmp_path, qapp):
    runtime = Application(tmp_path / "settings.json", native=False)
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        dialog.scale.slider.setValue(150)
        dialog.mini_scale.slider.setValue(125)
        dialog.mini_alpha.slider.setValue(35)
        dialog.minimum.toggle.setChecked(True)
        dialog.minimum.slider.setValue(10)
        dialog.maximum.toggle.setChecked(True)
        dialog.maximum.slider.setValue(90)
        rules = dialog.integrations_editor.system_rules
        rules.set_rules((IntegrationRule("test", "profile_1"),))
        rules.table.cellWidget(0, 2).setValue(15)
        rules.table.cellWidget(0, 3).setValue(85)
        assert rules.rules()[0].min_soc == 15 and rules.rules()[0].max_soc == 85
        dialog.save()
        stored = runtime.controller.store.load()
        assert (stored.scale, stored.mini_scale, stored.mini_transparency, stored.background_opacity) == (1.5, 1.25, 0.65, 0.82)
        assert (stored.manual_min, stored.manual_max) == (10, 90)
        dialog.scale.slider.setValue(250)
        dialog.load_draft()
        assert dialog.scale.slider.value() == dialog.scale.value() == 150
        dialog.minimum.toggle.setChecked(False)
        assert dialog.gather().manual_min is None
        dialog.reject()
        assert runtime.controller.store.load() == stored
    finally:
        runtime.stop()
        dialog.deleteLater()
        runtime.window.deleteLater()
        runtime.mini.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("color,text", [("#ffffff", "#000000"), ("#ffff00", "#000000"), ("#000000", "#ffffff"), ("#0000ff", "#ffffff")])
def test_accent_chooses_readable_text(color, text):
    assert accent_text(color) == text


def test_accent_fallback_when_windows_api_fails(monkeypatch):
    def unavailable(*args):
        raise OSError("Unavailable")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=SimpleNamespace(DwmGetColorizationColor=unavailable)))
    assert reference_effects.get_accent_color() == reference_effects.DEFAULT_ACCENT_COLOR


def test_shared_icon_contains_all_sizes(qapp):
    icon = app_icon()
    assert not icon.isNull()
    assert {size.width() for size in icon.availableSizes()} == {16, 24, 32, 48, 64, 128, 256}
    assert all(not icon.pixmap(size, size).isNull() for size in (16, 32, 48, 256))

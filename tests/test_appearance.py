import ctypes
from ctypes import wintypes
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QStyle, QStyleOptionSlider
import pytest
from qt_helpers import close_runtime, wait_until

from ecoflow_monitor.adapters import reference_effects
from ecoflow_monitor.application import Application
from ecoflow_monitor.integrations import IntegrationRule
from ecoflow_monitor.ui.common import app_icon
from ecoflow_monitor.ui.dashboard import Dashboard
from ecoflow_monitor.ui.integrations import ProfileDialog
from ecoflow_monitor.ui.percent_editor import PercentEditor
from ecoflow_monitor.ui.settings_window import SCALE_PREVIEW_DELAY_MS, SettingsWindow
from ecoflow_monitor.ui.theme import accent_text, settings_style
from ecoflow_monitor.ui.tokens import COLORS


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
        assert dialog.gather() == before
        assert dialog.tabs.currentIndex() == 2 and dialog.appearance_tabs.currentIndex() == 0
        assert profile.label.text() == "Unsaved profile"
        assert editor.combo() == "ctrl+f12"
        runtime.accent.refresh()
        assert spy.count() == 1
        dialog.show()
        qapp.processEvents()
        runtime.stop()
        monkeypatch.setattr(reference_effects, "get_accent_color", lambda: "#aa33dd")
        qapp.paletteChanged.emit(qapp.palette())
        qapp.processEvents()
        assert spy.count() == 1
    finally:
        profile.deleteLater()
        dialog.deleteLater()
        close_runtime(runtime, qapp)


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
        from ecoflow_monitor.ui.rules import RuleDialog
        rule_dialog = RuleDialog(rules.profiles, rules.conditions, False, rules.rules()[0], dialog)
        rule_dialog.minimum.setValue(15)
        rule_dialog.maximum.setValue(85)
        rule_dialog.save()
        rules.set_rules((rule_dialog.rule,))
        rule_dialog.deleteLater()
        assert rules.rules()[0].min_soc == 15 and rules.rules()[0].max_soc == 85
        dialog.save()
        stored = runtime.controller.store.load()
        assert (stored.scale, stored.widget.scale, stored.widget.opacity) == (1.5, 1.25, 0.35)
        assert (stored.manual_min, stored.manual_max) == (10, 90)
        dialog.scale.slider.setValue(250)
        dialog.load_draft()
        assert dialog.scale.slider.value() == dialog.scale.value() == 150
        dialog.minimum.toggle.setChecked(False)
        assert dialog.gather().manual_min is None
        dialog.reject()
        assert runtime.controller.store.load() == stored
    finally:
        dialog.deleteLater()
        close_runtime(runtime, qapp)


def test_scale_preview_waits_for_release_and_leaves_other_drafts_responsive(tmp_path, qapp, monkeypatch):
    runtime = Application(tmp_path / "settings.json", native=False)
    try:
        runtime.open_settings()
        dashboard = runtime.dashboard
        dashboard.navigation.setCurrentRow(2)
        applied = []
        apply = Dashboard.apply_appearance

        def record_appearance(window):
            previous = window._scale
            apply(window)
            if window._scale != previous:
                applied.append(window._scale)

        monkeypatch.setattr(Dashboard, "apply_appearance", record_appearance)
        slider = dashboard.scale.slider
        slider.setSliderDown(True)
        slider.setSliderPosition(125)
        slider.setSliderPosition(129)
        assert dashboard.scale.value() == 129
        assert "font-size:21px" in dashboard.appearance_examples["dashboard_scale"].sample.text()
        dashboard.dashboard_alpha.setValue(70)
        wait_until(qapp, lambda: runtime.controller.ui_settings.dashboard_opacity == 0.7)
        QTest.qWait(SCALE_PREVIEW_DELAY_MS + 50)
        assert applied == []
        slider.setSliderDown(False)
        assert runtime.controller.ui_settings.scale == 1
        wait_until(qapp, lambda: runtime.controller.ui_settings.scale == 1.29)
        assert applied == [1.29]
        assert runtime.controller.store.load().scale == 1

        widget_scale = dashboard.main_scale
        for _ in range(3):
            QTest.keyClick(widget_scale.slider, Qt.Key.Key_Right)
            QTest.qWait(90)
        assert runtime.controller.ui_settings.widget.scale == 1
        dashboard.hide()
        QTest.qWait(SCALE_PREVIEW_DELAY_MS + 50)
        assert runtime.controller.ui_settings.widget.scale == 1
        dashboard.show()
        wait_until(qapp, lambda: runtime.controller.ui_settings.widget.scale == 1.03)
        dashboard.scale.setValue(150)
        dashboard.reject()
        QTest.qWait(SCALE_PREVIEW_DELAY_MS + 50)
        assert runtime.controller.preview is None
        assert dashboard.scale.value() == 100

        dashboard.scale.setValue(129)
        line = dashboard.scale.spin.lineEdit()
        dashboard.scale.spin.selectAll()
        QTest.keyClicks(line, "1")
        assert not dashboard.scale.spin.hasAcceptableInput()
        QTest.qWait(SCALE_PREVIEW_DELAY_MS + 50)
        assert runtime.controller.preview is None
        QTest.keyClicks(line, "50")
        assert dashboard.scale.value() == 150
        dashboard.save()
        assert runtime.controller.store.load().scale == 1.5
        assert runtime.controller.preview is None
    finally:
        close_runtime(runtime, qapp)


@pytest.mark.parametrize("scale", [1, 1.25, 1.29, 1.5, 2])
@pytest.mark.design
def test_percent_slider_paints_round_handle_at_fractional_scale(qapp, scale):
    editor = PercentEditor(default=50)
    editor.setStyleSheet(settings_style(scale=scale))
    editor.resize(520, 140)
    editor.show()
    try:
        slider = editor.slider
        for enabled, focus in [(True, False), (True, True), (False, False)]:
            slider.setEnabled(enabled)
            slider.setFocus() if focus else slider.clearFocus()
            qapp.processEvents()
            option = QStyleOptionSlider()
            slider.initStyleOption(option)
            handle = slider.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                                   QStyle.SubControl.SC_SliderHandle, slider)
            image = slider.grab().toImage()
            ratio = image.devicePixelRatio()
            fill = QColor(COLORS["accent" if enabled else "subtle"])
            assert image.pixelColor(round(handle.center().x() * ratio), round(handle.center().y() * ratio)) == fill
            assert image.pixelColor(round((handle.right() - 1) * ratio), round((handle.top() + 1) * ratio)) != fill
    finally:
        editor.close()
        editor.deleteLater()
@pytest.mark.parametrize("color,text", [("#ffffff", "#000000"), ("#ffff00", "#000000"), ("#000000", "#ffffff"), ("#0000ff", "#ffffff")])
@pytest.mark.design
def test_accent_chooses_readable_text(color, text):
    assert accent_text(color) == text


def test_accent_fallback_when_windows_api_fails(monkeypatch):
    def unavailable(*args):
        raise OSError("Unavailable")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=SimpleNamespace(DwmGetColorizationColor=unavailable)))
    assert reference_effects.get_accent_color() == reference_effects.DEFAULT_ACCENT_COLOR


@pytest.mark.design
def test_shared_icon_contains_all_sizes(qapp):
    icon = app_icon()
    assert not icon.isNull()
    assert {size.width() for size in icon.availableSizes()} == {16, 24, 32, 48, 64, 128, 256}
    assert all(not icon.pixmap(size, size).isNull() for size in (16, 32, 48, 256))

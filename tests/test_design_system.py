from dataclasses import replace
import time

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton
import pytest
from qt_helpers import close_runtime, wait_until

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, METRICS, OverlayPreferences, SettingsStore
from ecoflow_monitor.ui.common import metric_text
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor
from ecoflow_monitor.ui.tokens import COLORS
from ecoflow_monitor.integrations import CliCommand, IntegrationDefinition, IntegrationProfile, IntegrationRule, Reaction
from ecoflow_monitor.ui.integrations import CommandDialog


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    close_runtime(instance, qapp)
    i18n.setup("en")


def test_dashboard_opens_on_start_and_preserves_draft_on_activation(runtime, qapp):
    runtime.start()
    dashboard = runtime.dashboard
    assert dashboard.isVisible()
    assert dashboard.tabs.count() == dashboard.navigation.count() == 5
    assert dashboard.page_keys[dashboard.navigation.currentRow()] == "monitoring"
    dashboard.navigation.setCurrentRow(1)
    dashboard.capacity.setValue(3456)
    dashboard._draft_changed()
    assert dashboard.save_button.isEnabled()
    dashboard.navigation.setCurrentRow(4)
    runtime.activate_existing_window()
    assert runtime.dashboard is dashboard
    assert dashboard.capacity.value() == 3456
    assert runtime.controller.settings.capacity_wh == 2048
    dashboard.reject()
    dashboard._draft_changed()
    assert dashboard.gather(False) == runtime.controller.settings
    assert not dashboard.save_button.isEnabled()


def test_preview_cancel_and_save_do_not_write_temporary_appearance(runtime, qapp):
    runtime.open_settings()
    dashboard = runtime.dashboard
    baseline = runtime.controller.settings
    dashboard.mini_alpha.setValue(33)
    dashboard._draft_changed()
    assert runtime.controller.settings == baseline
    assert runtime.controller.ui_settings.widget.opacity == pytest.approx(0.33)
    dashboard.reject()
    assert dashboard.gather(False) == runtime.controller.settings
    assert runtime.controller.preview is None
    dashboard.mini_alpha.setValue(41)
    dashboard._draft_changed()
    dashboard.save()
    assert dashboard.isVisible()
    assert runtime.controller.settings.widget.opacity == pytest.approx(0.41)
    assert runtime.controller.store.load().widget.opacity == pytest.approx(0.41)
    dashboard._draft_changed()
    assert not dashboard.save_button.isEnabled()


def test_dashboard_summary_order_and_opacity_follow_draft_and_save(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    baseline = runtime.controller.settings
    dashboard.dashboard_alpha.setValue(0)
    dashboard.dashboard_summary_order.down.click()
    dashboard._draft_changed()
    order = ("input_power", "battery_level", "output_power")
    assert runtime.controller.ui_settings.dashboard_opacity == 0
    assert dashboard.summary.order == order
    assert runtime.controller.store.load().dashboard_opacity == 1
    dashboard.reject()
    assert dashboard.summary.order == baseline.dashboard_summary_order
    assert runtime.controller.ui_settings.dashboard_opacity == 1

    dashboard.dashboard_alpha.setValue(50)
    dashboard.dashboard_summary_order.down.click()
    dashboard.save()
    stored = runtime.controller.store.load()
    assert stored.dashboard_opacity == 0.5
    assert stored.dashboard_summary_order == order
    assert stored.widget == baseline.widget


def test_dashboard_summary_shows_telemetry_outside_monitoring_and_localized_timestamp(runtime, monkeypatch):
    runtime.open_dashboard()
    dashboard, controller = runtime.dashboard, runtime.controller
    controller.apply_settings(replace(controller.settings, language="uk"))
    monkeypatch.setattr(Controller, "telemetry_source", property(lambda self: "https_fallback"))
    controller.telemetry = Telemetry(0, 185, 72, 1795780800, time.monotonic())
    controller.connection = ConnectionState.LIVE
    controller.changed.emit()

    assert dashboard.summary.isHidden()
    for index in range(1, dashboard.tabs.count()):
        dashboard.navigation.setCurrentRow(index)
        assert dashboard.summary.isVisible()
        assert dashboard.summary.readings["input_power"].value.text() == "0\u00a0Вт"
        assert dashboard.summary.readings["battery_level"].value.text() == "72\u00a0%"
        tooltip = dashboard.summary.status.toolTip()
        assert i18n.t("telemetry_active_fallback") in tooltip
        timestamp = tooltip.splitlines()[1].split(": ", 1)[-1]
        assert "?" not in timestamp
        assert timestamp[-6] in "+-" and timestamp[-3] == ":"
    dashboard.navigation.setCurrentRow(0)
    assert dashboard.summary.isHidden()


@pytest.mark.design
@pytest.mark.parametrize("language,scale,width", [("en", 1, 1050), ("uk", 1.5, 640)])
def test_dashboard_summary_readings_keep_their_slots_as_telemetry_changes(runtime, qapp, monkeypatch, language, scale, width):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language=language, scale=scale))
    runtime.open_dashboard()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    dashboard.setMinimumWidth(0)
    dashboard.resize(width, 750)
    summary = dashboard.summary
    reading_state = Controller.reading_state
    monkeypatch.setattr(Controller, "reading_state", lambda self, key: "loading" if self.telemetry is None else reading_state(self, key))
    controller.connection = ConnectionState.CONNECTING
    controller.changed.emit()
    qapp.processEvents()

    def slots():
        readings = tuple((reading.geometry(), reading.value.font().pixelSize()) for reading in summary.readings.values())
        return summary.mapTo(dashboard, QPoint()), readings

    baseline = slots()
    assert summary.readings["input_power"].loader.isVisible()
    for watts, source, state in [(0, "mqtt", ConnectionState.LIVE), (1850, "https_fallback", ConnectionState.LIVE),
                                 (85, "https", ConnectionState.STALE)]:
        monkeypatch.setattr(Controller, "telemetry_source", property(lambda self: source))
        controller.telemetry = Telemetry(watts, 185, 72, 1795780800, time.monotonic())
        controller.connection = state
        controller.changed.emit()
        qapp.processEvents()
        assert summary.readings["input_power"].value.text() == metric_text(controller, "input_power")
        assert slots() == baseline


@pytest.mark.design
def test_dashboard_opacity_percent_editor_preserves_visible_text_and_selection(runtime, qapp):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    editor = dashboard.dashboard_alpha
    dashboard.appearance_scroll.ensureWidgetVisible(editor)
    spin = editor.spin
    line = spin.lineEdit()
    for opacity in (87, 50, 0, 100):
        editor.slider.setValue(opacity)
        line.setFocus()
        line.setSelection(0, len(str(opacity)))
        dashboard._draft_changed()
        qapp.processEvents()
        assert line.hasFocus()
        assert line.selectedText() == str(opacity)
        assert line.text() == f"{opacity} %"
        assert spin.rect().contains(line.geometry())
        assert line.width() >= line.fontMetrics().horizontalAdvance(line.text()) + 4
    QTest.keyClick(line, Qt.Key.Key_Home)
    spin.selectAll()
    QTest.keyClicks(line, "65")
    QTest.keyClick(line, Qt.Key.Key_Return)
    assert editor.slider.value() == editor.value() == 65


@pytest.mark.design
@pytest.mark.parametrize("opacity,alpha", [(0, 0), (50, 192), (100, 255)])
def test_monitoring_nested_backgrounds_compose_and_balance_readings_use_card_padding(runtime, qapp, opacity, alpha):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    dashboard.dashboard_alpha.setValue(opacity)
    dashboard._draft_changed()
    dashboard.navigation.setCurrentRow(0)
    monitoring = dashboard.monitoring
    scroll = dashboard.tabs.currentWidget()
    for card in (monitoring.balance_card, monitoring.port_sections["inputs"][0]):
        qapp.processEvents()
        point = card.mapTo(monitoring, QPoint(8, 28))
        scroll.ensureVisible(point.x(), point.y(), 20, 20)
        qapp.processEvents()
        point = monitoring.mapTo(dashboard, point)
        image = dashboard.grab().toImage()
        ratio = image.devicePixelRatio()
        pixel = image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))
        assert abs(pixel.alpha() - alpha) <= (1 if card is not monitoring.balance_card else 0)
    expected = monitoring.cards[0].layout().contentsMargins().left()
    for reading in monitoring.balance_readings.values():
        margins = reading.box.contentsMargins()
        assert margins.left() == margins.top() == margins.right() == margins.bottom() == expected


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("opacity,alpha", [(0, 0), (50, 128), (100, 255)])
def test_dashboard_shell_alpha_preserves_control_surfaces(runtime, qapp, language, opacity, alpha):
    runtime.controller.apply_settings(replace(runtime.controller.settings, language=language))
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    dashboard.dashboard_alpha.setValue(opacity)
    dashboard._draft_changed()
    qapp.processEvents()
    image = dashboard.grab().toImage()
    ratio = image.devicePixelRatio()
    assert image.pixelColor(round((dashboard.width() - 22) * ratio), round(85 * ratio)).alpha() == alpha
    if opacity == 0:
        assert image.pixelColor(round((dashboard.width() - 4) * ratio), round(85 * ratio)).alpha() in (1, 2)
    assert dashboard.windowOpacity() == 1
    assert dashboard.close_button.grab().toImage().pixelColor(6, 12).alpha() == 255


@pytest.mark.design
def test_dashboard_summary_compacts_then_restores_full_status(runtime, qapp, monkeypatch):
    runtime.open_dashboard()
    dashboard = runtime.dashboard
    runtime.controller.telemetry = Telemetry(0, 185, 72, 1795780800, time.monotonic())
    runtime.controller.connection = ConnectionState.LIVE
    source = "mqtt"
    monkeypatch.setattr(Controller, "telemetry_source", property(lambda self: source))
    dashboard.navigation.setCurrentRow(4)
    dashboard.setMinimumWidth(0)
    summary = dashboard.summary
    title_margins = dashboard.titlebar.layout().contentsMargins()
    shell_margins = dashboard.shell.layout().contentsMargins()
    insets = title_margins.left() + title_margins.right() + shell_margins.left() + shell_margins.right()
    wrapped_width = insets + summary.reading_width() + summary.status.full_size_hint().width()
    dashboard.resize(wrapped_width - 1, 600)
    qapp.processEvents()
    assert summary.status.indicator_only
    dashboard.resize(wrapped_width, 600)
    qapp.processEvents()
    assert summary.parentWidget() is dashboard.shell
    assert not summary.status.indicator_only, (dashboard.width(), wrapped_width, summary.reading_width(),
                                                summary.status.full_size_hint().width(), summary.status.display_text())
    assert "MQTT" in summary.status.text()
    source = "https_fallback"
    runtime.controller.changed.emit()
    assert i18n.t("telemetry_active_fallback") in summary.status.accessibleName()
    for width in range(wrapped_width, 800, 10):
        dashboard.resize(width, 650)
        qapp.processEvents()
        if summary.status.indicator_only and summary.parentWidget() is dashboard.titlebar:
            break
    else:
        pytest.fail("The inline title bar never showed a compact connection indicator")
    dashboard.resize(1050, 720)
    qapp.processEvents()
    assert not summary.status.indicator_only
    assert i18n.t("telemetry_fallback_short") in summary.status.text()
    assert dashboard.titlebar.rect().contains(summary.geometry())
    assert summary.status.width() == summary.status.full_size_hint().width()
    assert dashboard.close_button.x() - summary.geometry().right() >= 24


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.29, 1.5, 2])
def test_dashboard_summary_and_appearance_examples_fit_at_scale(runtime, qapp, language, scale):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language=language, scale=scale))
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    qapp.processEvents()
    summary = dashboard.summary
    assert summary.parentWidget().rect().contains(summary.geometry())
    items = [*(reading.geometry() for reading in summary.readings.values()), summary.status.geometry()]
    assert all(summary.rect().contains(rect) for rect in items)
    assert all(not rect.intersects(other) for index, rect in enumerate(items) for other in items[index + 1:])
    sample = dashboard.appearance_examples["dashboard_opacity"]
    assert sample.isVisible()
    assert sample.rect().contains(sample.sample.geometry())
    scroll = dashboard.appearance_scroll
    scroll_body = scroll.widget()
    bar = scroll.verticalScrollBar()
    general_page = dashboard.appearance_tabs.widget(0)
    language_help = next(button for button in general_page.findChildren(QPushButton)
                         if button.text() == i18n.t("language_guide"))

    def scroll_reaches_last_example(example):
        bottom = example.mapTo(scroll_body, QPoint(0, example.height())).y()
        margin = scroll_body.layout().contentsMargins().bottom()
        extra = bar.maximum() + scroll.viewport().height() - bottom
        return extra >= 0 and (bar.maximum() == 0 or extra <= margin + round(32 * scale))

    for width, height in ((700, 650), (1134, 989)):
        dashboard.resize(width, height)
        for index, key in ((0, "dashboard"), (1, "widget"), (0, "dashboard")):
            dashboard.appearance_tabs.setCurrentIndex(index)
            last_key = "dashboard_summary_order" if index == 0 else "widget_metric_order"
            last = dashboard.appearance_examples[last_key]
            wait_until(qapp, lambda: last.isVisible() and scroll_reaches_last_example(last))
            example = dashboard.appearance_examples[key + "_opacity"]
            layout = example.parentWidget().parentWidget().layout()
            assert layout.itemAt(0).geometry().top() == layout.contentsMargins().top()
            assert example.isVisible()
            assert example.height() <= max(example.sizeHint().height(), example.heightForWidth(example.width()))
            if index == 0:
                language_bottom = dashboard.language.mapTo(scroll_body, QPoint(0, dashboard.language.height())).y()
                help_top = language_help.mapTo(scroll_body, QPoint()).y()
                assert 0 < help_top - language_bottom <= round(32 * scale)
    order = dashboard.dashboard_summary_order
    example = dashboard.appearance_examples["dashboard_summary_order"]
    dashboard.appearance_scroll.ensureWidgetVisible(example)
    qapp.processEvents()
    group = example.parentWidget()
    layout = group.parentWidget().layout()
    caption = layout.itemAt(layout.indexOf(group) - 1).widget()
    assert 0 < group.y() - caption.geometry().bottom() <= round(32 * scale)
    assert order.items.y() == 0
    assert order.items.viewport().rect().contains(order.items.visualItemRect(order.items.item(order.items.count() - 1)))
    assert 0 < order.up.geometry().top() - order.items.geometry().bottom() <= round(12 * scale)
    assert 0 < example.y() - order.geometry().bottom() <= round(12 * scale)


@pytest.mark.design
def test_hover_example_cycles_and_respects_reduced_motion_and_visibility(runtime, qapp, monkeypatch):
    now = [0.0]
    controller: Controller = runtime.controller
    monkeypatch.setattr(controller, "clock", lambda: now[0])
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.navigation.setCurrentRow(2)
    dashboard.monitor_values_on_hover.setChecked(True)
    example = dashboard.appearance_examples["dashboard_hover"]
    dashboard.tabs.currentWidget().ensureWidgetVisible(example)
    qapp.processEvents()
    hover = dashboard.hover_examples
    hover.refresh()
    assert hover.timer.isActive()
    assert i18n.t("appearance_resting") in example.sample.text()
    now[0] += 1.5
    hover.refresh()
    assert i18n.t("appearance_hovered") in example.sample.text()
    original = qapp.property("system_animations")
    try:
        qapp.setProperty("system_animations", False)
        hover.refresh()
        assert not hover.timer.isActive()
        assert i18n.t("appearance_resting") in example.sample.text()
        assert i18n.t("appearance_hovered") in example.sample.text()
    finally:
        qapp.setProperty("system_animations", original)
    dashboard.navigation.setCurrentRow(0)
    hover.refresh()
    assert not hover.timer.isActive()


@pytest.mark.parametrize("version", [0, 1, 2])
def test_legacy_migration_preserves_opacity_and_explicit_empty_integrations(tmp_path, version):
    payload = AppSettings().to_dict()
    payload.pop("workspace")
    payload["version"] = version
    payload["appearance"]["background_opacity"] = 0.37
    payload["mini_widget"]["transparency"] = 0.71
    payload["integrations"] = []
    payload["hotkeys"] = [{"action": "toggle_monitor", "combo": "ctrl+f9", "enabled": True}]
    settings = AppSettings.from_dict(payload)
    assert settings.background_opacity == 0.37
    assert settings.mini_transparency == 0.71
    assert settings.hotkeys[0].combo == "ctrl+f9"
    assert any(binding.action == "recover_widgets" for binding in settings.hotkeys)
    if version == 2:
        assert settings.integrations == ()
    store = SettingsStore(tmp_path / "settings.json")
    store.save(settings)
    assert store.load() == settings
    assert settings.to_dict()["version"] == 4


def test_zero_missing_and_stale_are_shared_by_all_views(runtime):
    runtime.open_dashboard()
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 120, 70, time.time(), time.monotonic())
    controller.connection = ConnectionState.LIVE
    controller.changed.emit()
    assert metric_text(controller, "input_power") == "0\u00a0W"
    assert runtime.dashboard.monitor_readings["input_power"].value.text() == "0\u00a0W"
    assert runtime.dashboard.monitor_readings["input_power"].value.text() == "0\u00a0W"
    assert runtime.mini.labels["input_power"].text() == "0\u00a0W"
    controller.connection = ConnectionState.STALE
    controller.changed.emit()
    assert runtime.mini.labels["output_power"].text() == "120\u00a0W"
    assert i18n.t("stale_data") in runtime.dashboard.connection_status.text()
    assert i18n.t("last_update").split("{")[0] in runtime.dashboard.connection_status.toolTip()
    controller.telemetry = None
    controller.changed.emit()
    assert runtime.dashboard.monitor_readings["input_power"].value.text() == "—"


def test_pin_lock_and_click_through_remain_independent_and_recoverable(runtime):
    runtime.mini.overlay.change(pinned=False)
    assert not runtime.controller.settings.widget.overlay.pinned
    runtime.mini.overlay.change(position_locked=True)
    assert runtime.controller.settings.widget.overlay.position_locked
    assert not runtime.mini.overlay.change(click_through=True)
    assert not runtime.controller.settings.widget.overlay.click_through
    runtime.controller.update_preferences(widget=replace(runtime.controller.settings.widget, overlay=replace(runtime.controller.settings.widget.overlay, click_through=True)))
    assert not runtime.window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    runtime.recover_widgets()
    assert not runtime.controller.settings.widget.overlay.click_through
    assert runtime.controller.settings.widget.overlay.position_locked
    assert not runtime.controller.settings.widget.overlay.pinned


def test_toolbar_preferences_do_not_get_overwritten_by_open_draft(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.capacity.setValue(3000)
    runtime.mini.overlay.change(pinned=False)
    assert not dashboard.overlay_fields["mini"]["pinned"].isChecked()
    dashboard.save()
    assert not runtime.controller.settings.widget.overlay.pinned
    assert runtime.controller.settings.capacity_wh == 3000


def test_material_controls_and_keyboard_metric_order(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.materials["mini"].setCurrentIndex(2)
    assert dashboard.gather().widget.opacity == 0
    dashboard._restore_overlay("mini")
    assert dashboard.gather().widget.opacity == pytest.approx(0.82)
    dashboard.materials["mini"].setCurrentIndex(0)
    assert dashboard.gather().widget.opacity == 1
    key = dashboard.metrics.order[0]
    dashboard.metrics.select(key)
    dashboard._move_metric(1)
    assert dashboard.metrics.order[1] == key
    assert set(dashboard.gather().widget.metric_order) == set(METRICS)


def test_hotkey_recording_escape_preserves_previous_shortcut(qapp):
    editor = HotkeyComboEditor("ctrl+f8")
    try:
        editor.start_recording()
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert editor.combo() == "ctrl+f8"
        editor.start_recording()
        QTest.keyClick(editor, Qt.Key.Key_F9, Qt.KeyboardModifier.ControlModifier)
        assert editor.combo() == "ctrl+f9"
    finally:
        editor.deleteLater()


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [0.5, 1, 1.25, 1.5, 2, 3])
@pytest.mark.design
def test_widgets_fit_screen_with_all_metrics(runtime, qapp, language, scale):
    settings = replace(runtime.controller.settings, widget=replace(runtime.controller.settings.widget, scale=scale, enabled=True, small_metrics=METRICS[:3], large_metrics=METRICS[:3], expanded_metrics=METRICS[3:]), language=language, scale=scale)
    runtime.controller.apply_settings(settings)
    runtime.window.show()
    runtime.mini.reveal_button.setChecked(True)
    qapp.processEvents()
    for window in (runtime.window, runtime.mini):
        assert window.screen().availableGeometry().contains(window.geometry())
    for key in runtime.widget.composition.visible:
        reading = runtime.widget.readings[key]
        assert runtime.mini.body.rect().contains(reading.geometry())


@pytest.mark.design
def test_canonical_tokens_are_packaged_and_match_reference():
    assert COLORS["accent"] == "#74E3BD"
    assert COLORS["input"] == "#84D5FF"
    assert COLORS["output"] == "#FFC884"


@pytest.mark.design
def test_font_fallback_renders_ukrainian_and_numbers(runtime):
    from PySide6.QtGui import QFontMetrics
    metrics = QFontMetrics(runtime.dashboard.brand.font())
    assert all(metrics.inFontUcs4(ord(char)) for char in "EcoFlow 72% Україна Її Єє Ґґ")


def test_disabled_manual_override_retains_value_after_save_and_restart(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.minimum.setValue(27)
    dashboard.minimum.toggle.setChecked(False)
    dashboard.save()
    stored = runtime.controller.store.load()
    assert stored.manual_min is None
    assert stored.manual_min_retained == 27
    dashboard.load_draft()
    dashboard.minimum.toggle.setChecked(True)
    assert dashboard.minimum.value() == 27


def test_invalid_form_preserves_input_and_disables_save(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.minimum.setValue(95)
    dashboard.maximum.setValue(20)
    dashboard._draft_changed()
    assert dashboard.minimum.value() == 95
    assert dashboard.maximum.value() == 20
    assert dashboard.error_label.text()
    assert not dashboard.save_button.isEnabled()
    assert runtime.controller.settings.manual_min is None


def test_existing_mini_order_survives_migration_and_unedited_save(runtime):
    payload = runtime.controller.settings.to_dict()
    payload["version"] = 2
    payload.pop("workspace")
    payload["mini_widget"]["metrics"] = ["output_power", "battery_level"]
    payload["mini_widget"]["hidden_metrics"] = ["maximum_charge_level", "input_power"]
    runtime.controller.apply_settings(AppSettings.from_dict(payload))
    runtime.open_settings()
    assert runtime.dashboard.gather().mini_metrics == ("output_power", "battery_level")
    assert runtime.dashboard.gather().mini_hidden_metrics == ("maximum_charge_level", "input_power")


def test_rule_dependencies_block_condition_disable_and_keep_rules(runtime):
    runtime.open_settings()
    editor = runtime.dashboard.integrations_editor
    rules = (IntegrationRule("input_rule", "profile_1", input_condition="present"),)
    editor.system_rules.set_rules(rules)
    editor.conditions["input"].setChecked(False)
    assert editor.conditions["input"].isChecked()
    assert editor.system_rules.rules() == rules
    assert "input_rule" in editor.condition_error.text()


def test_command_editor_preview_preserves_id_and_never_executes(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: calls.append(args))
    command = CliCommand("quiet", "Quiet", ("--name", "two words"))
    module = IntegrationDefinition("module", "Program", mapping=(command,))
    dialog = CommandDialog(module, command)
    try:
        assert dialog.identity.isReadOnly()
        assert not dialog.save_button.isEnabled()
        dialog.label.setText("Updated name")
        assert dialog.save_button.isEnabled()
        assert '"two words"' in dialog.preview.text()
        dialog.save()
        assert dialog.command.id == "quiet"
        assert dialog.command.arguments == command.arguments
        assert calls == []
    finally:
        dialog.deleteLater()


def test_reduced_motion_stops_energy_animation(runtime, qapp):
    runtime.controller.telemetry = Telemetry(100, 50, 70, time.time(), time.monotonic())
    runtime.controller.connection = ConnectionState.LIVE
    runtime.window.show()
    qapp.setProperty("system_animations", False)
    runtime.dashboard.monitoring.balance.refresh()
    assert not runtime.motion_driver._timer.isActive()
    qapp.setProperty("system_animations", True)


@pytest.mark.design
def test_packaged_inter_is_the_actual_font_for_controls_and_keycaps(runtime):
    from pathlib import Path
    from PySide6.QtGui import QFontInfo, QFontMetrics
    from ecoflow_monitor.ui import common
    dashboard = runtime.dashboard
    assert (Path(common.__file__).parent / "assets/fonts/Inter.ttf").is_file()
    assert QFontInfo(dashboard.capacity.font()).family() == "Inter"
    assert QFontInfo(dashboard.brand.font()).family() == "Inter"
    assert all(QFontMetrics(dashboard.brand.font()).inFontUcs4(ord(char)) for char in "ЇїЄєҐґІі 0123456789")
    runtime.controller.apply_settings(replace(runtime.controller.settings, language="uk"))
    editor = dashboard.hotkey_rows["recover_widgets"][1]
    assert any(label.text() == "W" for label in editor.findChildren(QLabel))
    header = dashboard.integrations_editor.system_rules.table.horizontalHeaderItem(0)
    assert header.text() == i18n.t("rule_enabled")


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
@pytest.mark.design
def test_metric_visibility_rows_and_numeric_text_fit_after_scale_change(runtime, qapp, scale):
    dashboard = runtime.dashboard
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale, language="uk"))
    dashboard.show()
    qapp.processEvents()
    dashboard.resize(round(1184 * scale), round(900 * scale))
    dashboard.navigation.setCurrentRow(2)
    dashboard.appearance_tabs.setCurrentIndex(1)
    qapp.processEvents()
    for row in dashboard.metrics.rows.values():
        for control in row.controls.values():
            assert row.rect().contains(control.geometry())
            assert control.height() >= control.fontMetrics().height()
    spin = dashboard.mini_alpha.spin
    assert spin.lineEdit().height() >= spin.fontMetrics().height()
    assert spin.lineEdit().width() >= spin.lineEdit().fontMetrics().horizontalAdvance(spin.text())
    assert spin.lineEdit().text()
    page = dashboard.tabs.currentWidget()
    assert page.horizontalScrollBar().maximum() == 0
    assert set(dashboard.metrics.order) == set(METRICS)


def test_metric_reordering_does_not_move_other_groups_or_lose_draft(runtime):
    editor = runtime.dashboard.metrics
    editor.set_mode("estimated_runtime", "expanded")
    editor.set_mode("maximum_charge_level", "expanded")
    always = editor.group("always")
    assert editor.move_before("maximum_charge_level", "estimated_runtime")
    assert editor.group("expanded") == ("maximum_charge_level", "estimated_runtime")
    assert editor.move_before("maximum_charge_level", "estimated_runtime", after=True)
    assert editor.group("expanded") == ("estimated_runtime", "maximum_charge_level")
    assert editor.group("always") == always
    assert editor.move_before("battery_level", "estimated_runtime")
    assert editor.modes["battery_level"] == "small"
    assert editor.modes["estimated_runtime"] == "expanded"
    runtime.dashboard.save()
    stored = runtime.controller.store.load()
    assert stored.widget.expanded_metrics == editor.group("expanded")
    assert stored.widget.small_metrics == editor.group("small")


def test_shortcut_conflict_preserves_working_combo_and_distinguishes_numpad(runtime):
    editor = runtime.dashboard.hotkey_rows["toggle_mini"][1]
    enabled = runtime.dashboard.hotkey_rows["toggle_mini"][2]
    enabled.setChecked(True)
    editor.set_combo("ctrl+f8")
    editor.start_recording()
    editor.assign("ctrl+alt+num5")
    assert editor.combo() == "ctrl+f8"
    assert editor.property("state") == "conflict"
    editor.start_recording()
    editor.assign("ctrl+alt+5")
    assert editor.combo() == "ctrl+alt+5"
    assert editor.property("state") == "assigned"


@pytest.mark.design
def test_keyboard_focus_never_outlines_page_containers(runtime, qapp):
    dashboard = runtime.dashboard
    dashboard.show()
    dashboard.navigation.setCurrentRow(1)
    dashboard.connection_tabs.setCurrentIndex(1)
    dashboard.activateWindow()
    qapp.processEvents()
    page = dashboard.tabs.currentWidget()
    page.setFocus()
    dashboard._focus_binding.keyboard = True
    dashboard._focus_binding.refresh()
    assert dashboard._focus_binding.outline.isHidden()
    assert dashboard.access.isVisible()
    dashboard.access.setFocus(Qt.FocusReason.TabFocusReason)
    dashboard._focus_binding.refresh()
    assert not dashboard._focus_binding.outline.isHidden()
    assert dashboard._focus_binding.outline.width() < page.width()
    dashboard.summary.readings["battery_level"].setFocus(Qt.FocusReason.TabFocusReason)
    dashboard._focus_binding.refresh()
    assert not dashboard._focus_binding.outline.isHidden()


def test_main_flags_do_not_affect_app_shell_and_recover_input_without_shortcut(runtime, qapp):
    previous = runtime.controller.settings
    settings = replace(previous, widget=replace(previous.widget, opacity=0, overlay=OverlayPreferences(pinned=True, position_locked=True, click_through=True)), transparency=True)
    runtime.controller.apply_settings(settings)
    runtime.start()
    dashboard = runtime.dashboard
    assert not dashboard.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert not dashboard.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    dashboard.capacity.setValue(3000)
    dashboard.save()
    stored = runtime.controller.store.load()
    assert stored.widget.overlay == replace(settings.widget.overlay, click_through=False)
    assert stored.widget.opacity == 0
    assert stored.capacity_wh == 3000
    assert "main" in dashboard.materials
    assert "main" in dashboard.overlay_fields


def test_new_startup_opens_only_app_shell_and_keeps_both_widgets_disabled(runtime):
    runtime.start()
    windows = [widget for widget in runtime.app.topLevelWidgets() if getattr(widget, "controller", None) is runtime.controller]
    assert set(windows) == {runtime.dashboard, runtime.mini, runtime.main}
    assert not runtime.main.isVisible() and not runtime.mini.isVisible()
    assert runtime.window is runtime.dashboard
    assert not runtime.dashboard.error_label.text()
    assert not runtime.dashboard.save_button.isEnabled()


def test_disabling_capture_restores_combo_and_unblocks_other_valid_edits(runtime):
    dashboard = runtime.dashboard
    editor = dashboard.hotkey_rows["toggle_monitor"][1]
    editor.start_recording()
    dashboard.hotkey_rows["toggle_monitor"][2].setChecked(False)
    assert not editor.recording
    assert editor.combo() == "ctrl+alt+num5"
    dashboard.capacity.setValue(3000)
    dashboard._draft_changed()
    assert dashboard.save_button.isEnabled()


def test_key_picker_can_change_modifiers_and_key_in_one_menu(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QCheckBox
    editor = HotkeyComboEditor("ctrl+f8")
    errors = []
    def choose():
        menu = QApplication.activePopupWidget()
        try:
            next(box for box in menu.findChildren(QCheckBox) if box.text() == "Alt").setChecked(True)
            menu.search.setText("F24")
            QTest.keyClick(menu.search, Qt.Key.Key_Return)
        except (StopIteration, AttributeError) as error:
            errors.append(error)
        finally:
            if menu is not None:
                menu.close()
    try:
        editor.show()
        QTimer.singleShot(0, choose)
        editor.choose_key()
        assert not errors
        assert editor.combo() == "ctrl+alt+f24"
    finally:
        editor.deleteLater()


@pytest.mark.design
def test_footer_actions_stay_reachable_in_a_small_window_at_large_scale(runtime, qapp):
    dashboard = runtime.dashboard
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=3, language="uk"))
    dashboard.show()
    dashboard.resize(640, 480)
    dashboard.navigation.setCurrentRow(1)
    qapp.processEvents()
    footer = dashboard.footer
    footer.ensureWidgetVisible(dashboard.save_button)
    qapp.processEvents()
    position = dashboard.save_button.mapTo(footer.viewport(), dashboard.save_button.rect().topLeft())
    assert footer.viewport().rect().intersects(dashboard.save_button.rect().translated(position))
    assert footer.horizontalScrollBar().maximum() == 0
    assert dashboard.width() <= dashboard.screen().availableGeometry().width()


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
@pytest.mark.design
def test_all_footer_buttons_fit_reference_width_and_keep_control_height(runtime, qapp, scale):
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale, language="uk"))
    dashboard = runtime.dashboard
    dashboard.show()
    qapp.processEvents()
    dashboard.resize(round(1184 * scale), round(900 * scale))
    dashboard.navigation.setCurrentRow(1)
    qapp.processEvents()
    viewport = dashboard.footer.viewport()
    assert dashboard.footer.verticalScrollBar().maximum() == 0
    for button in dashboard.footer.buttons:
        point = button.mapTo(viewport, button.rect().topLeft())
        assert viewport.rect().contains(button.rect().translated(point))
        assert 35 * scale <= button.height() <= 45 * scale
        assert button.text()


@pytest.mark.design
def test_monitoring_and_controls_share_the_shell_effective_dpi_scale(runtime, monkeypatch):
    from PySide6.QtGui import QPaintDevice
    from ecoflow_monitor.ui.dashboard import Dashboard
    ratio = [2]
    monkeypatch.setattr(Dashboard, QPaintDevice.devicePixelRatioF.__name__, lambda self: ratio[0])
    runtime.controller.apply_settings(replace(runtime.controller.settings, scale=3))
    dashboard = runtime.dashboard
    assert dashboard._scale == 1.5
    assert dashboard.monitor_readings["battery_level"].value.font().pixelSize() == 54
    assert dashboard.capacity.font().pixelSize() == 21
    ratio[0] = 1
    dashboard.apply_appearance()
    assert dashboard._scale == 3
    assert dashboard.monitor_readings["battery_level"].value.font().pixelSize() == 108
    assert runtime.controller.settings.scale == 3


def test_integration_editor_accepts_command_and_profile_dialog_results(runtime, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from ecoflow_monitor.ui.integrations import CommandDialog, ProfileDialog
    editor = runtime.dashboard.integrations_editor
    original = editor.modules()[0]
    def accept_command(dialog):
        dialog.label.setText("Updated command")
        dialog.save()
        return QDialog.DialogCode.Accepted
    def accept_profile(dialog):
        dialog.label.setText("Updated profile")
        dialog.save()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(CommandDialog, QDialog.exec.__name__, accept_command)
    monkeypatch.setattr(ProfileDialog, QDialog.exec.__name__, accept_profile)
    editor.mapping.selectRow(0)
    editor.edit_mapping(True)
    updated = editor.modules()[0]
    assert updated.mapping[0].label == "Updated command"
    assert updated.mapping[0].id == original.mapping[0].id
    editor.profiles.selectRow(0)
    editor.edit_profile(True)
    updated = editor.modules()[0]
    assert updated.profiles[0].label == "Updated profile"
    assert updated.profiles[0].id == original.profiles[0].id
    assert runtime.controller.settings.integrations[0] == original


def test_window_geometry_is_not_persisted_by_temporary_preview(runtime):
    dashboard = runtime.dashboard
    baseline = runtime.controller.settings.dashboard_geometry
    dashboard.mini_alpha.setValue(33)
    dashboard._draft_changed()
    assert runtime.controller.preview is not None
    dashboard.resize(600, 650)
    dashboard._save_geometry()
    assert runtime.controller.settings.dashboard_geometry == baseline
    dashboard.reject()
    dashboard._save_geometry()
    assert runtime.controller.settings.dashboard_geometry[2:] == (800, 650)


def test_shutdown_saves_last_normal_geometry_without_reopening_windows(runtime):
    dashboard = runtime.dashboard
    dashboard.resize(600, 650)
    runtime.stop()
    assert runtime.controller.store.load().dashboard_geometry[2:] == (800, 650)
    assert dashboard.isHidden() and runtime.mini.isHidden()

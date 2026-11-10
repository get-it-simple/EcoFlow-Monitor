from dataclasses import replace
import time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
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
    instance.stop()
    instance.window.deleteLater()
    instance.mini.deleteLater()
    qapp.processEvents()
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
    dashboard.background_transparency.setValue(33)
    dashboard._draft_changed()
    assert runtime.controller.settings == baseline
    assert runtime.controller.ui_settings.background_opacity == 0.33
    dashboard.reject()
    assert dashboard.gather(False) == runtime.controller.settings
    assert runtime.controller.preview is None
    dashboard.background_transparency.setValue(41)
    dashboard._draft_changed()
    dashboard.save()
    assert dashboard.isVisible()
    assert runtime.controller.settings.background_opacity == 0.41
    assert runtime.controller.store.load().background_opacity == 0.41
    dashboard._draft_changed()
    assert not dashboard.save_button.isEnabled()


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
    assert settings.to_dict()["version"] == 3


def test_zero_missing_and_stale_are_shared_by_all_views(runtime):
    runtime.open_dashboard()
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 120, 70, time.time(), time.monotonic())
    controller.connection = ConnectionState.LIVE
    controller.changed.emit()
    assert metric_text(controller, "input_power") == "0\u00a0W"
    assert runtime.window.flow.readings["input_power"].value.text() == "0\u00a0W"
    assert runtime.dashboard.monitor_readings["input_power"].value.text() == "0\u00a0W"
    assert runtime.mini.labels["input_power"].text() == "0\u00a0W"
    controller.connection = ConnectionState.STALE
    controller.changed.emit()
    assert runtime.mini.labels["output_power"].text() == "120\u00a0W"
    assert i18n.t("stale_data") in runtime.window.status.text()
    assert i18n.t("last_update").split("{")[0] in runtime.window.status.toolTip()
    controller.telemetry = None
    controller.changed.emit()
    assert runtime.window.flow.readings["input_power"].value.text() == "—"


def test_pin_lock_and_click_through_remain_independent_and_recoverable(runtime):
    runtime.window.overlay.change(pinned=False)
    assert not runtime.controller.settings.main_overlay.pinned
    runtime.window.overlay.change(position_locked=True)
    assert runtime.controller.settings.main_overlay.position_locked
    assert not runtime.window.overlay.change(click_through=True)
    assert not runtime.controller.settings.main_overlay.click_through
    runtime.controller.update_preferences(main_overlay=replace(runtime.controller.settings.main_overlay, click_through=True))
    assert not runtime.window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    runtime.recover_widgets()
    assert not runtime.controller.settings.main_overlay.click_through
    assert runtime.controller.settings.main_overlay.position_locked
    assert not runtime.controller.settings.main_overlay.pinned


def test_toolbar_preferences_do_not_get_overwritten_by_open_draft(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.capacity.setValue(3000)
    runtime.window.overlay.change(pinned=False)
    assert not dashboard.overlay_fields["main"]["pinned"].isChecked()
    dashboard.save()
    assert not runtime.controller.settings.main_overlay.pinned
    assert runtime.controller.settings.capacity_wh == 3000


def test_material_controls_and_keyboard_metric_order(runtime):
    runtime.open_settings()
    dashboard = runtime.dashboard
    dashboard.materials["main"].setCurrentIndex(2)
    assert dashboard.gather().background_opacity == 0
    dashboard._restore_overlay("main")
    assert dashboard.gather().background_opacity == 0.82
    dashboard.materials["mini"].setCurrentIndex(0)
    assert dashboard.gather().mini_transparency == 0
    dashboard.metrics.setCurrentRow(0)
    key = dashboard.metrics.item(0).data(Qt.ItemDataRole.UserRole)
    dashboard._move_metric(1)
    assert dashboard.metrics.item(1).data(Qt.ItemDataRole.UserRole) == key
    assert set(dashboard.gather().metric_order) == set(METRICS)


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
def test_widgets_fit_screen_with_all_metrics(runtime, qapp, language, scale):
    settings = replace(runtime.controller.settings, language=language, scale=scale, mini_scale=scale, mini_enabled=True, mini_metrics=METRICS[:3], mini_hidden_metrics=METRICS[3:])
    runtime.controller.apply_settings(settings)
    runtime.window.show()
    runtime.mini.reveal_button.setChecked(True)
    qapp.processEvents()
    for window in (runtime.window, runtime.mini):
        assert window.screen().availableGeometry().contains(window.geometry())
    for reading in runtime.mini.readings.values():
        assert runtime.mini.body.rect().contains(reading.geometry())


def test_canonical_tokens_are_packaged_and_match_reference():
    assert COLORS["accent"] == "#74E3BD"
    assert COLORS["input"] == "#84D5FF"
    assert COLORS["output"] == "#FFC884"


def test_font_fallback_renders_ukrainian_and_numbers(runtime):
    from PySide6.QtGui import QFontMetrics
    metrics = QFontMetrics(runtime.window.title.font())
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
    runtime.window.flow.refresh()
    assert not runtime.window.flow._timer.isActive()
    qapp.setProperty("system_animations", True)

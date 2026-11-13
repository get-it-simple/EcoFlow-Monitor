import ast
from dataclasses import replace
import json
from pathlib import Path
import time

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtTest import QTest
import pytest
from qt_helpers import close_runtime

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.adapters.integrations import IntegrationObservation, TransitionResult
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.ui.common import metric_text, status_tooltip
from ecoflow_monitor.ui.common import effective_scale
from ecoflow_monitor.ui.settings_window import SettingsWindow


class NoCredentials:
    def read(self, reference):
        raise AssertionError("UI tests must not access credentials")


@pytest.mark.parametrize("conditions", [("input", "soc"), ("soc",), ("input",), ()])
@pytest.mark.design
def test_system_rule_fields_fill_available_table_width(qapp, conditions):
    from ecoflow_monitor.integrations import IntegrationRule, afterburner_example
    from ecoflow_monitor.ui.rules import RulesEditor
    editor = RulesEditor(False)
    profiles = afterburner_example().profiles
    editor.configure(profiles, conditions)
    editor.append(IntegrationRule("example", profiles[0].id, min_soc=20, max_soc=80))
    expected = editor.rules()
    try:
        editor.resize(1000, 300)
        editor.show()
        qapp.processEvents()
        table = editor.table
        visible = [column for column in range(table.columnCount()) if not table.isColumnHidden(column)]
        widths = {column: table.columnWidth(column) for column in visible}
        assert abs(sum(widths.values()) - table.viewport().width()) <= 1
        editor.resize(1400, 300)
        qapp.processEvents()
        assert abs(sum(table.columnWidth(column) for column in visible) - table.viewport().width()) <= 1
        assert all(table.columnWidth(column) > widths[column] for column in visible)
        assert editor.rules() == expected
    finally:
        editor.close()
        editor.deleteLater()


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("work_size", [(1920, 1800), (640, 480)])
@pytest.mark.parametrize("scale", [0.5, 1, 3])
@pytest.mark.design
def test_settings_initial_size_fits_content_or_scrolls(runtime, qapp, language, work_size, scale):
    from types import SimpleNamespace
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QLabel
    area = QRect(40, 30, *work_size)

    class ScreenConstrainedSettings(SettingsWindow):
        def screen(self):
            return SimpleNamespace(availableGeometry=lambda: area)

    runtime.controller.apply_settings(replace(runtime.controller.settings, language=language, scale=scale))
    dialog = ScreenConstrainedSettings(runtime.controller, runtime.window)
    scroller = dialog.tabs.widget(0)
    extra = QLabel("Additional settings content")
    extra.setMinimumHeight(650)
    scroller.widget().layout().insertWidget(0, extra)
    try:
        dialog.show()
        qapp.processEvents()
        assert area.contains(dialog.frameGeometry())
        if work_size[1] > 1000:
            for index in range(dialog.tabs.count()):
                dialog.tabs.setCurrentIndex(index)
                qapp.processEvents()
                page = dialog.tabs.currentWidget()
                if page.verticalScrollBar().maximum():
                    assert dialog.frameGeometry().height() >= area.height() - 2
                    page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
                    assert page.verticalScrollBar().value() == page.verticalScrollBar().maximum()
                assert page.horizontalScrollBar().maximum() == 0, (index, page.widget().minimumSizeHint(), page.viewport().size())
        else:
            assert scroller.verticalScrollBar().maximum() > 0
            scroller.verticalScrollBar().setValue(scroller.verticalScrollBar().maximum())
            assert scroller.verticalScrollBar().value() > 0
        assert runtime.controller.settings.scale == scale
    finally:
        dialog.reject()
        dialog.deleteLater()


@pytest.fixture
def runtime(tmp_path, qapp):
    controller = Controller(SettingsStore(tmp_path / "settings.json"), NoCredentials())
    instance = Application(controller.store.path, native=False, controller=controller)
    yield instance
    close_runtime(instance, qapp)
    i18n.setup("en")


def test_initial_view_hides_msi_and_displays_unknown_data(runtime):
    assert runtime.window.monitoring.msi_section.isHidden()
    assert all("—" in readout.display_text() for readout in runtime.window.monitoring.limits.readouts)
    assert metric_text(runtime.controller, "input_power") == "—"
    assert "No successful update" in status_tooltip(runtime.controller)


@pytest.mark.parametrize("state", ["hidden", "minimized", "visible"])
def test_repeat_launch_restores_monitor(runtime, qapp, state):
    window = runtime.window
    window.request_visibility(state != "hidden")
    if state == "minimized":
        window.showMinimized()
    runtime.activate_existing_window()
    qapp.processEvents()
    assert runtime.dashboard.isVisible()
    assert not runtime.dashboard.isMinimized()
    assert runtime.controller.settings.monitor_visible is True


def test_repeat_launch_preserves_settings_draft(runtime, qapp):
    runtime.open_settings()
    dialog = runtime.dashboard
    dialog.capacity.setValue(1234)
    runtime.window.request_visibility(False)
    runtime.activate_existing_window()
    qapp.processEvents()
    assert dialog.isVisible()
    assert dialog.capacity.value() == 1234
    assert runtime.controller.settings.capacity_wh != 1234
    assert runtime.dashboard is dialog
    dialog.reject()


@pytest.mark.parametrize("size", [(320, 310), (640, 620), (900, 620), (640, 900), (1280, 1240), (1920, 1860)])
@pytest.mark.design
def test_flow_metric_hover_follows_paint_scale_and_offsets(runtime, qapp, size):
    flow = runtime.window.monitoring
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 100, 75, time.time(), time.monotonic(), 20, 90)
    controller.connection = ConnectionState.STALE
    flow.setMinimumSize(0, 0)
    flow.resize(*size)
    flow.show()
    flow.refresh()
    qapp.processEvents()
    for key, field in (("input_power", "powInSumW"), ("output_power", "powOutSumW"), ("battery_level", "cmsBattSoc")):
        reading = flow.readings[key]
        assert field in reading.toolTip()
        assert reading.value.accessibleName() == reading.accessibleName()
        assert reading.parentWidget().rect().contains(reading.geometry())
    assert "calculated by this app" in flow.readings["estimated_runtime"].toolTip()


def test_metric_sources_refresh_in_monitor_mini_and_open_settings(runtime, qapp):
    from PySide6.QtCore import Qt
    from ecoflow_monitor.settings import METRICS
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, widget=replace(controller.settings.widget, small_metrics=METRICS, large_metrics=METRICS), manual_min=10))
    dialog = SettingsWindow(controller, runtime.window)
    try:
        def tips():
            item = dialog.metrics.rows["minimum_discharge_level"]
            return runtime.window.monitoring.limits.toolTip(), runtime.mini.labels["minimum_discharge_level"].parentWidget().toolTip(), item.toolTip()
        for tooltip in tips():
            assert "Source: Manual. Value: 10 %" in tooltip
            assert "do not change the station" in tooltip
        controller.telemetry = Telemetry(0, 100, 75, time.time(), time.monotonic(), 20, 90)
        controller.connection = ConnectionState.LIVE
        controller.changed.emit()
        for tooltip in tips():
            assert "Source: EcoFlow API. Value: 20 %" in tooltip
            assert "do not change the station" not in tooltip
        controller.telemetry = None
        controller.apply_settings(replace(controller.settings, manual_min=None))
        for tooltip in tips():
            assert "Source: Unknown. Value: —" in tooltip
        assert "local reference" in dialog.capacity.toolTip()
        assert "cmsMinDsgSoc" in dialog.minimum.slider.toolTip()
        assert "do not change the station" in dialog.minimum.toggle.toolTip()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_friendly_rule_names_and_explanations(runtime, qapp):
    from ecoflow_monitor.integrations import IntegrationRule
    from ecoflow_monitor.integrations import IntegrationProfile
    from ecoflow_monitor.ui.rules import RuleDialog
    from ecoflow_monitor.ui.metrics import API_FIELDS, metric_tooltip
    from ecoflow_monitor.adapters.ecoflow import PORT_KEYS
    assert i18n.t("rule_min") == "Charge from (%)"
    assert i18n.t("rule_max") == "Charge to (%)"
    assert "SOC" not in i18n.t("rule_hint")
    assert all(API_FIELDS[key] == field for key, field in PORT_KEYS.items())
    assert "cmsBattSoc" in metric_tooltip("battery_level")
    assert "not battery health" in " ".join(metric_tooltip("battery_level").split())
    for key in (*API_FIELDS, "estimated_runtime", "capacity_wh", "anderson_2"):
        assert "_help" not in metric_tooltip(key)
    for application in (False, True):
        editor = RuleDialog((IntegrationProfile("example", "Example"),), ("application", "input", "soc"), application,
                            IntegrationRule("test", "example", executable="example.exe" if application else "", min_soc=20, max_soc=80))
        try:
            condition = editor.input
            assert [condition.itemText(index) for index in range(condition.count())] == ["Any", "On", "Off"]
            explanation = " ".join(condition.toolTip().split())
            assert "without charging the battery" in explanation
            assert "does not switch" in explanation
            for index, value in enumerate(("any", "present", "absent")):
                condition.setCurrentIndex(index)
                assert editor.draft().input_condition == value
            assert "powInSumW" in editor.input.toolTip()
            assert "includes this" in editor.minimum.toolTip()
            assert "cmsBattSoc" in editor.minimum.spin.toolTip()
            assert "except 100%" in editor.maximum.slider.toolTip()
            assert editor.draft().min_soc == 20
            assert editor.draft().max_soc == 80
        finally:
            editor.deleteLater()


def test_power_units_follow_discovered_catalog_and_live_language_changes(runtime, tmp_path, monkeypatch):
    catalogs = tmp_path / "catalogs"
    english = catalogs / "en"
    translated = catalogs / "xx"
    english.mkdir(parents=True)
    translated.mkdir()
    package_file = i18n.__file__
    assert package_file is not None
    english_catalog = (Path(package_file).parent / "en" / "strings.json").read_text(encoding="utf-8")
    (english / "strings.json").write_text(english_catalog, encoding="utf-8")
    (translated / "strings.json").write_text(json.dumps({"unit_watt": "test-unit"}), encoding="utf-8")
    monkeypatch.setattr(i18n, "_translator", i18n.Translator(catalogs))
    controller = runtime.controller
    controller.telemetry = Telemetry(50, 280, 80, time.time(), time.monotonic())
    for language, unit in (("en", "W"), ("xx", "test-unit"), ("en", "W")):
        controller.apply_settings(replace(controller.settings, language=language))
        assert runtime.mini.labels["input_power"].text() == "50 " + unit
        assert runtime.mini.labels["output_power"].text() == "280 " + unit
        assert metric_text(controller, "battery_level") == "80 %"
    controller.telemetry = None
    assert metric_text(controller, "input_power") == "—"


def test_metric_tooltips_follow_language_changes(runtime, qapp):
    from ecoflow_monitor.ui.metrics import metric_tooltip
    controller = runtime.controller
    english = metric_tooltip("input_power")
    assert runtime.window.monitoring.readings["input_power"].toolTip() == english + "\n" + i18n.t("missing_data")
    controller.apply_settings(replace(controller.settings, language="uk"))
    translated = metric_tooltip("input_power")
    assert translated != english and "powInSumW" in translated
    assert runtime.mini.labels["input_power"].parentWidget().toolTip() == translated + "\n" + i18n.t("missing_data")
    assert runtime.window.monitoring.readings["input_power"].toolTip() == translated + "\n" + i18n.t("missing_data")
    controller.apply_settings(replace(controller.settings, language="en"))
    assert runtime.window.monitoring.readings["input_power"].toolTip() == english + "\n" + i18n.t("missing_data")


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [0.5, 1, 2, 3])
@pytest.mark.design
def test_settings_sections_expand_without_overlapping_rows(runtime, qapp, language, scale):
    controller = runtime.controller
    module = replace(controller.settings.integrations[0], enabled=True)
    controller.apply_settings(replace(controller.settings, language=language, scale=scale, integrations=(module,)))
    dialog = runtime.dashboard
    dialog.show()
    dialog.navigation.setCurrentRow(2)
    dialog.appearance_tabs.setCurrentIndex(1)
    qapp.processEvents()
    rows = list(dialog.metrics.rows.values())
    for row in rows:
        assert all(row.height() >= control.height() for control in row.controls.values())
        assert all(row.rect().contains(control.geometry()) for control in row.controls.values())
    for mode in ("always", "expanded", "off"):
        visible = [dialog.metrics.rows[key] for key in dialog.metrics.group(mode)]
        for above, below in zip(visible, visible[1:]):
            assert above.geometry().bottom() < below.geometry().top()
    dialog.scale.setValue(125)
    dialog.navigation.setCurrentRow(4)
    section = dialog.hotkey_sections[module.id]
    section.set_expanded(True)
    qapp.processEvents()
    fields = [field for name, (field, _, _) in dialog.hotkey_rows.items() if ":" in name]
    for above, below in zip(fields, fields[1:]):
        assert above.geometry().bottom() < below.geometry().top()
    name = "profile:afterburner:profile_3"
    dialog.hotkey_rows[name][1].set_combo("ctrl+shift+num3")
    section.set_expanded(False)
    dialog._refresh_hotkeys()
    assert dialog.hotkey_sections[module.id].body.isHidden()
    assert dialog.hotkey_rows[name][1].combo() == "ctrl+shift+num3"
    dialog.load_draft()
    assert dialog.gather().scale == scale


def test_profile_details_tooltips_update_on_selection_and_edit(runtime, qapp):
    from PySide6.QtCore import Qt
    from ecoflow_monitor.integrations import IntegrationRule
    from ecoflow_monitor.ui.integrations import IntegrationsEditor
    module = runtime.controller.settings.integrations[0]
    profiles = tuple(replace(profile, technical_details=f"Notes for {profile.id}\n70% <manual>") for profile in module.profiles)
    module = replace(module, enabled=True, profiles=profiles, system_rules=(IntegrationRule("example", profiles[0].id),))
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(module,)))
    window = runtime.window
    window.refresh()
    window.monitoring.profile_choice.setCurrentIndex(1)
    expected = profiles[1].label + "\n" + profiles[1].technical_details
    assert window.monitoring.profile_choice.toolTip() == expected
    assert window.monitoring.profile_choice.itemData(1, Qt.ItemDataRole.ToolTipRole) == expected
    editor = IntegrationsEditor()
    try:
        editor.load((module,))
        assert editor.profiles.item(1, 1).toolTip() == expected
        from ecoflow_monitor.ui.rules import RuleDialog
        rule_dialog = RuleDialog(profiles, module.conditions, False, module.system_rules[0], editor)
        combo = rule_dialog.profile
        combo.setCurrentIndex(1)
        assert combo.toolTip() == expected
        rule_dialog.deleteLater()
        updated = replace(module, profiles=tuple(replace(profile, technical_details="Updated") for profile in profiles))
        runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(updated,)))
        window.refresh()
        assert window.monitoring.profile_choice.toolTip() == profiles[1].label + "\nUpdated"
    finally:
        editor.deleteLater()


def test_helper_requires_consent_and_does_not_repeat_on_auto_toggle(runtime, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    module = replace(runtime.controller.settings.integrations[0], enabled=True)
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(module,)))
    submitted = []
    prompts = []
    monkeypatch.setattr(Controller, "_submit", lambda self, *args: submitted.append(args))
    from ecoflow_monitor.ui.common import elevation_prompt
    def create(*args):
        prompts.append(args)
        return elevation_prompt(*args)
    monkeypatch.setattr("ecoflow_monitor.application.elevation_prompt",
                        create)
    runtime._offer_elevation(module.id, automatic=True)
    qapp.processEvents()
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
    qapp.processEvents()
    assert len(prompts) == 1 and not submitted
    runtime.controller.set_auto(module.id, True)
    runtime._offer_elevation(module.id, automatic=True)
    assert len(prompts) == 1 and not submitted
    runtime._offer_elevation(module.id)
    qapp.processEvents()
    runtime.confirmations.dialog.done(QMessageBox.StandardButton.Yes)
    assert [item[0] for item in submitted] == ["elevate:" + module.id]


@pytest.mark.parametrize("settings_open", [False, True])
def test_helper_consent_stays_above_monitor_and_active_settings(runtime, qapp, monkeypatch, settings_open):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QMessageBox
    module = replace(runtime.controller.settings.integrations[0], enabled=True)
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(module,)))
    submitted = []
    monkeypatch.setattr(Controller, "_submit", lambda self, *args: submitted.append(args))
    runtime.window.startup()
    settings = SettingsWindow(runtime.controller, runtime.window) if settings_open else None
    if settings:
        settings.setWindowModality(Qt.WindowModality.ApplicationModal)
        settings.show()
        qapp.processEvents()
    seen = []
    def decline():
        prompt = qapp.activeModalWidget()
        if isinstance(prompt, QMessageBox):
            seen.append((prompt.parentWidget(), bool(prompt.windowFlags() & Qt.WindowType.WindowStaysOnTopHint),
                         prompt.windowModality(), prompt.defaultButton() == prompt.button(QMessageBox.StandardButton.No)))
            QTest.mouseClick(prompt.button(QMessageBox.StandardButton.No), Qt.MouseButton.LeftButton)
    try:
        runtime._offer_elevation(module.id)
        qapp.processEvents()
        decline()
        assert seen == [(settings or runtime.window, True, Qt.WindowModality.ApplicationModal, True)]
        assert not submitted
    finally:
        if settings:
            settings.close()
            settings.deleteLater()


def test_settings_are_drafts_and_reset_does_not_apply(runtime, qapp):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    dialog.scale.setValue(150)
    dialog.mini_enabled.setChecked(True)
    assert runtime.controller.settings.scale == 1
    assert runtime.controller.settings.widget.enabled is False
    dialog.load_draft()
    assert dialog.scale.value() == 100 and dialog.mini_enabled.isChecked() is False
    dialog.reject()
    dialog.deleteLater()


def test_settings_save_merges_background_auto_change(runtime):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True, auto_enabled=True),)))
    dialog = SettingsWindow(controller, runtime.window)
    dialog.scale.setValue(150)
    controller.set_auto("afterburner", False)
    dialog.save()
    assert controller.settings.scale == 1.5 and controller.module("afterburner").auto_enabled is False
    assert controller.store.load().scale == 1.5
    dialog.deleteLater()


def test_msi_enable_reveals_only_draft_controls_until_save(runtime):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    assert not dialog.integrations_editor.enabled.isChecked()
    dialog.integrations_editor.enabled.setChecked(True)
    assert dialog.integrations_editor.enabled.isChecked()
    assert runtime.controller.module("afterburner").enabled is False
    dialog.save()
    assert runtime.controller.module("afterburner").enabled is True
    assert not runtime.window.monitoring.msi_section.isHidden()
    dialog.deleteLater()


def test_mini_metric_selection_and_live_language_change(runtime, qapp):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, widget=replace(controller.settings.widget, enabled=True, small_metrics=("output_power", "battery_level"), large_metrics=("output_power", "battery_level"), metric_order=("output_power", "battery_level", "input_power", "estimated_runtime", "minimum_discharge_level", "maximum_charge_level")), language="uk"))
    assert tuple(runtime.mini.labels) == ("output_power", "battery_level")
    assert runtime.mini.isVisible()
    assert i18n.get_current_language() == "uk"
    assert runtime.dashboard.navigation.item(1).text() == i18n.t("connection_settings")


def test_flow_animation_stops_when_hidden_or_stale(runtime, qapp):
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 280, 80, time.time(), time.monotonic(), min_soc=20, max_soc=90)
    controller.connection = ConnectionState.LIVE
    runtime.window.show()
    runtime.window.refresh()
    qapp.processEvents()
    assert runtime.window.monitoring.balance.motion_active()
    controller.connection = ConnectionState.STALE
    runtime.window.refresh()
    assert not runtime.window.monitoring.balance.motion_active()
    assert metric_text(controller, "estimated_runtime") == "—"
    controller.connection = ConnectionState.LIVE
    runtime.window.refresh()
    runtime.window.hide()
    assert not runtime.motion_driver.visible(runtime.window.monitoring.balance)


def test_requested_profile_is_not_shown_as_confirmed(runtime):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True),)))
    controller.integration_states["afterburner"].observation = IntegrationObservation("integration_ready")
    controller.integration_states["afterburner"].requested_profile = "profile_3"
    runtime.window.refresh()
    assert "Requested profile: Profile 3" in runtime.window.monitoring.notifications.toolTip()
    assert "Current profile" not in runtime.window.monitoring.notifications.toolTip()


@pytest.mark.parametrize("result,expected", [
    (TransitionResult(True, 1, exit_code=0), "Command completed"),
    (TransitionResult(False, 0, "activation", 1, "integration_elevation_required", winerror=740), "Windows error: 740"),
    (TransitionResult(False, 0, "activation", 1, "integration_command_failed", exit_code=7), "Exit code: 7"),
])
def test_cli_result_is_shown_without_claiming_observed_gpu_state(runtime, result, expected):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True),)))
    state = controller.integration_states["afterburner"]
    state.observation = IntegrationObservation("integration_ready")
    state.requested_profile = "profile_5"
    state.result = result
    runtime.window.refresh()
    text = runtime.window.monitoring.notifications.toolTip()
    assert expected in text and "Requested profile: Profile 5" in text
    assert "Current profile" not in text and "unknown" not in text
    assert not runtime.window.monitoring.notifications.isHidden()


def test_every_literal_translation_key_exists_in_english():
    package = Path(i18n.__file__).resolve().parent.parent
    catalog = json.loads((package / "i18n" / "en" / "strings.json").read_text(encoding="utf-8"))
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value in catalog, f"Unknown translation in {path.name}: {node.args[0].value}"


@pytest.mark.parametrize("scale", [0.5, 1, 2, 3])
@pytest.mark.design
def test_scale_applies_to_labels_and_keeps_widget_status_accessible(runtime, qapp, scale):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, widget=replace(controller.settings.widget, enabled=True, scale=scale), scale=scale))
    runtime.window.show()
    qapp.processEvents()
    actual = runtime.window._scale
    assert actual <= effective_scale(runtime.window, scale)
    assert runtime.window.titlebar.title.font().pixelSize() == round(16 * actual)
    assert runtime.widget.status.isHidden()
    assert runtime.widget.status_indicator.toolTip()
    assert runtime.widget.status_indicator.accessibleName() == runtime.widget.status_indicator.toolTip()
    dialog = SettingsWindow(controller, runtime.window)
    dialog.show()
    qapp.processEvents()
    assert dialog.capacity.font().pixelSize() == dialog.font().pixelSize()
    assert 6 <= dialog.capacity.font().pixelSize() <= round(14 * effective_scale(dialog, scale))
    assert dialog.screen().availableGeometry().contains(dialog.geometry())
    assert controller.settings.scale == scale
    dialog.close()
    dialog.deleteLater()


def test_scale_roundtrip_does_not_accumulate_window_height(runtime, qapp):
    runtime.window.resize(800, 700)
    for _ in range(3):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=0.5))
        qapp.processEvents()
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=1))
        qapp.processEvents()
    assert runtime.window.width() == 800
    assert runtime.window.height() == 700


def test_zooming_past_screen_then_back_preserves_logical_geometry(runtime, qapp):
    runtime.window.resize(800, 700)
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale))
        qapp.processEvents()
    assert runtime.window.width() == 800
    assert runtime.window.height() == 700


def test_saved_scaled_geometry_is_not_scaled_again_on_startup(tmp_path, qapp, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QScreen
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 2400, 1800))
    store = SettingsStore(tmp_path / "scaled.json")
    from ecoflow_monitor.settings import AppSettings
    store.save(replace(AppSettings(), scale=1.5, dashboard_geometry=(0, 0, 1400, 1000)))
    controller = Controller(store, NoCredentials())
    instance = Application(store.path, native=False, controller=controller)
    try:
        qapp.processEvents()
        assert instance.window.width() == 1400 and instance.window.height() == 1000
    finally:
        close_runtime(instance, qapp)


@pytest.mark.design
def test_first_msi_enable_fits_two_line_profile_controls(runtime, qapp):
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(replace(runtime.controller.settings.integrations[0], enabled=True),)))
    runtime.open_dashboard()
    page = runtime.dashboard.monitoring
    page.msi_section.set_expanded(True)
    qapp.processEvents()
    assert page.integration_selector.isVisible()
    assert page.auto_toggle.accessibleName() == i18n.t("automatic_mode")
    assert page.profile_choice.height() >= page.profile_choice.minimumSizeHint().height()
    assert page.apply_profile.height() >= page.apply_profile.minimumSizeHint().height()


@pytest.mark.parametrize("enabled,opacity,alpha", [(True, 140 / 255, 140), (False, 0.25, 255), (True, 0, 0), (True, 0.25, 64), (True, 1, 255)])
@pytest.mark.design
def test_monitor_background_rendering_includes_scroll_and_msi_containers(runtime, qapp, enabled, opacity, alpha):
    runtime.controller.apply_settings(replace(runtime.controller.settings, widget=replace(runtime.controller.settings.widget, opacity=opacity), transparency=enabled, integrations=(replace(runtime.controller.settings.integrations[0], enabled=True),)))
    runtime.open_dashboard()
    window = runtime.dashboard
    window.monitoring.msi_section.set_expanded(True)
    qapp.processEvents()
    image = window.grab().toImage()
    assert window.windowOpacity() == 1
    assert image.pixelColor(4, window.height() // 2).alpha() == 255
    assert not window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert runtime.controller.settings.widget.opacity == opacity


@pytest.mark.parametrize("enabled", [False, True])
def test_transparency_draft_save_reset_and_restart(runtime, qapp, enabled):
    dialog = runtime.dashboard
    baseline = runtime.controller.settings
    dialog.materials["mini"].setCurrentIndex(1 if enabled else 0)
    assert runtime.controller.settings == baseline
    dialog.load_draft()
    assert dialog.gather().widget.opacity == baseline.widget.opacity
    dialog.materials["mini"].setCurrentIndex(1 if enabled else 0)
    dialog.save()
    stored = runtime.controller.store.load()
    assert stored.widget.opacity == pytest.approx(0.82 if enabled else 1)
    assert stored.background_opacity == baseline.background_opacity
    restarted = Controller(runtime.controller.store, NoCredentials())
    try:
        assert restarted.settings.widget.opacity == stored.widget.opacity
        assert restarted.settings.widget.overlay == baseline.widget.overlay
    finally:
        restarted.stop()


def test_background_transparency_draft_reset_save_and_cancel(runtime, qapp):
    dialog = runtime.dashboard
    baseline = runtime.controller.settings
    dialog.mini_alpha.setValue(75)
    dialog._draft_changed()
    assert runtime.controller.settings == baseline
    assert runtime.controller.ui_settings.widget.opacity == pytest.approx(0.75)
    dialog.load_draft()
    assert runtime.controller.preview is None
    dialog.mini_alpha.setValue(75)
    dialog.save()
    stored = runtime.controller.store.load()
    assert stored.widget.opacity == pytest.approx(0.75)
    assert stored.background_opacity == baseline.background_opacity
    dialog.mini_alpha.setValue(10)
    dialog.reject()
    assert runtime.controller.store.load() == stored


@pytest.mark.parametrize("visible", [False, True])
def test_startup_preserves_visibility_without_temporary_show(runtime, visible):
    runtime.controller.update_preferences(monitor_visible=visible)
    runtime.start()
    assert runtime.dashboard.isVisible()
    assert runtime.dashboard is runtime.window
    assert not any(widget.objectName() == "monitor_window" for widget in runtime.app.topLevelWidgets())
    assert runtime.controller.settings.monitor_visible is visible


def test_rapid_visibility_requests_and_transparency_changes_use_latest_request(runtime, qapp):
    window = runtime.dashboard
    window.capacity.setValue(3000)
    for enabled in (False, True, False, True):
        runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=enabled))
        window.request_visibility(True)
        window.request_visibility(False)
    qapp.processEvents()
    assert window.isHidden()
    window.request_visibility(True)
    assert window.capacity.value() == 3000


@pytest.mark.parametrize("visible", [False, True])
def test_shutdown_prevents_later_window_show(runtime, qapp, visible):
    window = runtime.dashboard
    window.request_visibility(visible)
    runtime.stop()
    window.startup()
    window.request_visibility(True)
    qapp.processEvents()
    assert window.isHidden()
    assert not window._geometry_timer.isActive()
    assert not runtime.motion_driver._timer.isActive()


@pytest.mark.parametrize("enabled,alpha", [(True, 209), (False, 255)])
@pytest.mark.design
def test_background_survives_repeated_show_and_native_handle_recreation(runtime, qapp, enabled, alpha):
    runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=enabled))
    window = runtime.window
    window.show()
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == 255
    window.hide()
    window.destroy()
    window.show()
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == 255

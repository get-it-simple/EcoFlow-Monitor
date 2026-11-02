import ast
from dataclasses import replace
import json
from pathlib import Path
import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint
from PySide6.QtTest import QTest
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.adapters.integrations import IntegrationObservation, TransitionResult
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.ui.common import metric_text, status_tooltip
from ecoflow_monitor.ui.common import effective_scale
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui import window as window_module


class NoCredentials:
    def read(self, reference):
        raise AssertionError("UI tests must not access credentials")


@pytest.fixture
def runtime(tmp_path, qapp):
    controller = Controller(SettingsStore(tmp_path / "settings.json"), NoCredentials())
    instance = Application(controller.store.path, native=False, controller=controller)
    yield instance
    instance.stop()
    instance.window.deleteLater()
    instance.mini.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    i18n.setup("en")


def test_initial_view_hides_msi_and_displays_unknown_data(runtime):
    assert runtime.window.msi_section.isHidden()
    assert "—" in runtime.window.limits.text()
    assert metric_text(runtime.controller, "input_power") == "—"
    assert "No successful update" in status_tooltip(runtime.controller)


@pytest.mark.parametrize("size", [(320, 310), (640, 620), (900, 620), (640, 900), (1280, 1240), (1920, 1860)])
def test_flow_metric_hover_follows_paint_scale_and_offsets(runtime, qapp, size: tuple[int, int]):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip
    flow = runtime.window.flow
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 100, 75, time.time(), time.monotonic(), 20, 90,
                                     (("usb_c1", 10), ("usb_a1", 5), ("anderson_1", 30)))
    controller.connection = ConnectionState.STALE
    flow.setMinimumSize(0, 0)
    flow.resize(*size)
    scale = min(size[0] / 640, size[1] / 620)
    offset = QPointF((size[0] - 640 * scale) / 2, (size[1] - 620 * scale) / 2)
    for point, field in (((100, 40), "powInSumW"), ((500, 40), "powOutSumW"),
                         ((130, 116), "powGetAcIn"), ((510, 116), "powGetPv"),
                         ((130, 530), "powGetAcOut"), ((510, 530), "powGet12v"),
                         ((320, 300), "cmsBattSoc"), ((180, 427), "cmsMinDsgSoc"),
                         ((180, 224), "cmsMaxChgSoc"), ((120, 583), "powGetTypec1"),
                         ((320, 583), "powGetQcusb1"), ((520, 583), "powGet12vList.powGet12vItem")):
        position = (offset + QPointF(*point) * scale).toPoint()
        event = QHelpEvent(QEvent.Type.ToolTip, position, flow.mapToGlobal(position))
        QCoreApplication.sendEvent(flow, event)
        assert field in QToolTip.text()
    assert "calculated by this app" in flow.tooltip_at(offset + QPointF(320, 400) * scale)
    assert flow.tooltip_at(offset + QPointF(10, 450) * scale) == ""
    QToolTip.hideText()


def test_metric_sources_refresh_in_monitor_mini_and_open_settings(runtime, qapp):
    from PySide6.QtCore import Qt
    from ecoflow_monitor.settings import METRICS
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, mini_metrics=METRICS, manual_min=10))
    dialog = SettingsWindow(controller, runtime.window)
    try:
        def tips():
            item = next(dialog.metrics.item(index) for index in range(dialog.metrics.count())
                        if dialog.metrics.item(index).data(Qt.ItemDataRole.UserRole) == "minimum_discharge_level")
            return runtime.window.limits.toolTip(), runtime.mini.labels["minimum_discharge_level"].parentWidget().toolTip(), item.toolTip()
        for tooltip in tips():
            assert "Source: Manual. Value: 10 %" in tooltip
            assert "do not change the station" in tooltip
        controller.telemetry = Telemetry(0, 100, 75, time.time(), time.monotonic(), 20, 90)
        controller.connection = ConnectionState.LIVE
        controller.changed.emit()
        for tooltip in tips():
            assert "Source: EcoFlow API. Value: 20 %" in tooltip
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
    from ecoflow_monitor.ui.rules import RulesEditor
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
        editor = RulesEditor(application)
        try:
            editor.append(IntegrationRule("test", "example", min_soc=20, max_soc=80))
            column = 2 if application else 1
            condition = editor.table.cellWidget(0, column)
            assert editor.table.horizontalHeaderItem(column).text() == "Input"
            assert [condition.itemText(index) for index in range(condition.count())] == ["Any", "On", "Off"]
            explanation = " ".join(condition.toolTip().split())
            assert "without charging the battery" in explanation
            assert "does not switch" in explanation
            for index, value in enumerate(("any", "present", "absent")):
                condition.setCurrentIndex(index)
                assert editor.rules()[0].input_condition == value
            assert "powInSumW" in editor.table.horizontalHeaderItem(column).toolTip()
            assert "includes this" in editor.table.horizontalHeaderItem(column + 1).toolTip()
            assert "except 100%" in editor.table.horizontalHeaderItem(column + 2).toolTip()
            assert "cmsBattSoc" in editor.table.cellWidget(0, column + 1).spin.toolTip()
            assert "except 100%" in editor.table.cellWidget(0, column + 2).slider.toolTip()
            assert editor.rules()[0].min_soc == 20
            assert editor.rules()[0].max_soc == 80
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
        assert runtime.mini.labels["input_power"].text() == "50 " + unit
        assert runtime.mini.labels["output_power"].text() == "280 " + unit
        assert metric_text(controller, "battery_level") == "80 %"
    controller.telemetry = None
    assert metric_text(controller, "input_power") == "—"


def test_metric_tooltips_follow_language_changes(runtime, qapp):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip
    from ecoflow_monitor.ui.metrics import metric_tooltip
    from ecoflow_monitor.settings import METRICS
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, mini_metrics=METRICS))
    english = metric_tooltip("battery_level")
    flow = runtime.window.flow
    scale = min(flow.width() / 640, flow.height() / 620)
    position = QPointF((flow.width() - 640 * scale) / 2 + 100 * scale,
                      (flow.height() - 620 * scale) / 2 + 40 * scale).toPoint()
    QCoreApplication.sendEvent(flow, QHelpEvent(QEvent.Type.ToolTip, position, flow.mapToGlobal(position)))
    assert QToolTip.text() == metric_tooltip("input_power")
    controller.apply_settings(replace(controller.settings, language="uk"))
    translated = metric_tooltip("battery_level")
    assert translated != english
    assert "cmsBattSoc" in translated
    assert runtime.mini.labels["battery_level"].parentWidget().toolTip() == translated
    assert QToolTip.text() == metric_tooltip("input_power")
    controller.apply_settings(replace(controller.settings, language="en"))
    assert runtime.mini.labels["battery_level"].parentWidget().toolTip() == english
    assert QToolTip.text() == metric_tooltip("input_power")
    QToolTip.hideText()


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [0.5, 1, 2, 3])
def test_settings_sections_expand_without_overlapping_rows(runtime, qapp, language, scale):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel
    controller = runtime.controller
    module = replace(controller.settings.integrations[0], enabled=True)
    controller.apply_settings(replace(controller.settings, language=language, scale=scale, integrations=(module,)))
    dialog = SettingsWindow(controller, runtime.window)
    try:
        dialog.show()
        dialog.tabs.setCurrentIndex(2)
        qapp.processEvents()
        for section in dialog.appearance_sections.values():
            assert section.body.isHidden()
            QTest.mouseClick(section.header, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        sections = list(dialog.appearance_sections.values())
        for above, below in zip(sections, sections[1:]):
            assert above.geometry().bottom() < below.geometry().top()
        scroller = dialog.tabs.currentWidget()
        assert scroller.widget().height() >= scroller.widget().minimumSizeHint().height()
        dialog.scale.setValue(125)
        sections[0].set_expanded(False)
        sections[0].set_expanded(True)
        assert dialog.scale.value() == 125
        dialog.tabs.setCurrentIndex(3)
        section = dialog.hotkey_sections[module.id]
        assert section.body.isHidden()
        QTest.mouseClick(section.header, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        fields = [field for name, (field, _, _) in dialog.hotkey_rows.items() if ":" in name]
        for above, below in zip(fields, fields[1:]):
            assert above.geometry().bottom() < below.geometry().top()
        for field in fields:
            label = field.findChild(QLabel)
            assert label.height() >= label.heightForWidth(label.width())
        name = "profile:afterburner:profile_3"
        dialog.hotkey_rows[name][1].set_combo("ctrl+shift+num3")
        section.set_expanded(False)
        dialog._refresh_hotkeys()
        assert dialog.hotkey_sections[module.id].body.isHidden()
        assert dialog.hotkey_rows[name][1].combo() == "ctrl+shift+num3"
        dialog.load_draft()
        assert dialog.gather().scale == scale
    finally:
        dialog.reject()
        dialog.deleteLater()


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
    window.profile_choice.setCurrentIndex(1)
    expected = profiles[1].label + "\n" + profiles[1].technical_details
    assert window.profile_choice.toolTip() == expected
    assert window.profile_choice.itemData(1, Qt.ItemDataRole.ToolTipRole) == expected
    editor = IntegrationsEditor()
    try:
        editor.load((module,))
        assert editor.profiles.item(1).toolTip() == expected
        combo = editor.system_rules.table.cellWidget(0, 4)
        combo.setCurrentIndex(1)
        assert combo.toolTip() == expected
        updated = replace(module, profiles=tuple(replace(profile, technical_details="Updated") for profile in profiles))
        runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(updated,)))
        window.refresh()
        assert window.profile_choice.toolTip() == profiles[1].label + "\nUpdated"
    finally:
        editor.deleteLater()


def test_helper_requires_consent_and_does_not_repeat_on_auto_toggle(runtime, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from types import SimpleNamespace
    module = replace(runtime.controller.settings.integrations[0], enabled=True)
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(module,)))
    submitted = []
    prompts = []
    monkeypatch.setattr(Controller, "_submit", lambda self, *args: submitted.append(args))
    def decline(*args):
        prompts.append(args)
        return QMessageBox.StandardButton.No
    monkeypatch.setattr("ecoflow_monitor.application.elevation_prompt",
                        lambda *args: SimpleNamespace(exec=decline, deleteLater=lambda: None))
    runtime._offer_elevation(module.id, automatic=True)
    assert len(prompts) == 1 and not submitted
    runtime.controller.set_auto(module.id, True)
    runtime._offer_elevation(module.id, automatic=True)
    assert len(prompts) == 1 and not submitted
    monkeypatch.setattr("ecoflow_monitor.application.elevation_prompt",
                        lambda *args: SimpleNamespace(exec=lambda: QMessageBox.StandardButton.Yes, deleteLater=lambda: None))
    runtime._offer_elevation(module.id)
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
    QTimer.singleShot(0, decline)
    try:
        runtime._offer_elevation(module.id)
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
    assert runtime.controller.settings.mini_enabled is False
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
    assert not runtime.window.msi_section.isHidden()
    dialog.deleteLater()


def test_mini_metric_selection_and_live_language_change(runtime, qapp):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language="uk", mini_enabled=True, mini_metrics=("output_power", "battery_level")))
    assert tuple(runtime.mini.labels) == ("output_power", "battery_level")
    assert runtime.mini.isVisible()
    assert i18n.get_current_language() == "uk"
    assert runtime.window.settings_button.toolTip() != "Settings"


def test_flow_animation_stops_when_hidden_or_stale(runtime, qapp):
    controller = runtime.controller
    controller.telemetry = Telemetry(0, 280, 80, time.time(), time.monotonic(), min_soc=20, max_soc=90)
    controller.connection = ConnectionState.LIVE
    runtime.window.show()
    runtime.window.refresh()
    qapp.processEvents()
    assert runtime.window.flow._timer.isActive()
    controller.connection = ConnectionState.STALE
    runtime.window.refresh()
    assert not runtime.window.flow._timer.isActive()
    assert metric_text(controller, "estimated_runtime") == "—"
    controller.connection = ConnectionState.LIVE
    runtime.window.refresh()
    runtime.window.hide()
    assert not runtime.window.flow._timer.isActive()


def test_requested_profile_is_not_shown_as_confirmed(runtime):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, integrations=(replace(controller.settings.integrations[0], enabled=True),)))
    controller.integration_states["afterburner"].observation = IntegrationObservation("integration_ready")
    controller.integration_states["afterburner"].requested_profile = "profile_3"
    runtime.window.refresh()
    assert "Requested profile: Profile 3" in runtime.window.profile_status.text()
    assert "Current profile" not in runtime.window.profile_status.text()


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
    text = runtime.window.profile_status.text()
    assert expected in text and "Requested profile: Profile 5" in text
    assert "Current profile" not in text and "unknown" not in text
    assert runtime.window.profile_status.toolTip() == text


def test_every_literal_translation_key_exists_in_english():
    package = Path(i18n.__file__).resolve().parent.parent
    catalog = json.loads((package / "i18n" / "en" / "strings.json").read_text(encoding="utf-8"))
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value in catalog, f"Unknown translation in {path.name}: {node.args[0].value}"


@pytest.mark.parametrize("scale", [0.5, 1, 2, 3])
def test_scale_applies_to_labels_and_preserves_mini_status_space(runtime, qapp, scale):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, scale=scale, mini_enabled=True, mini_scale=scale))
    runtime.window.show()
    qapp.processEvents()
    actual = runtime.window._scale
    assert actual <= effective_scale(runtime.window, scale)
    assert runtime.window.title.font().pixelSize() == round(17 * actual)
    assert runtime.mini.status.height() >= runtime.mini.status.fontMetrics().height()
    dialog = SettingsWindow(controller, runtime.window)
    dialog.show()
    qapp.processEvents()
    assert dialog.capacity.font().pixelSize() == dialog.font().pixelSize()
    assert 6 <= dialog.capacity.font().pixelSize() <= round(13 * effective_scale(dialog, scale))
    assert dialog.screen().availableGeometry().contains(dialog.geometry())
    assert controller.settings.scale == scale
    dialog.close()
    dialog.deleteLater()


def test_scale_roundtrip_does_not_accumulate_window_height(runtime, qapp):
    runtime.window.resize(600, 700)
    for _ in range(3):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=0.5))
        qapp.processEvents()
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=1))
        qapp.processEvents()
    assert runtime.window.width() == 600
    assert runtime.window.height() == 700


def test_zooming_past_screen_then_back_preserves_logical_geometry(runtime, qapp):
    runtime.window.resize(600, 700)
    for scale in (0.5, 3, 1):
        runtime.controller.apply_settings(replace(runtime.controller.settings, scale=scale))
        qapp.processEvents()
    assert runtime.window.width() == 600
    assert runtime.window.height() == 700


def test_saved_scaled_geometry_is_not_scaled_again_on_startup(tmp_path, qapp):
    store = SettingsStore(tmp_path / "scaled.json")
    from ecoflow_monitor.settings import AppSettings
    store.save(replace(AppSettings(), scale=1.5, monitor_geometry=(0, 0, 700, 800)))
    controller = Controller(store, NoCredentials())
    instance = Application(store.path, native=False, controller=controller)
    try:
        qapp.processEvents()
        assert instance.window.width() == 700 and instance.window.height() == 800
    finally:
        instance.stop()
        instance.window.deleteLater()
        instance.mini.deleteLater()


def test_first_msi_enable_fits_two_line_profile_controls(runtime, qapp):
    runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(replace(runtime.controller.settings.integrations[0], enabled=True),)))
    runtime.window.show()
    qapp.processEvents()
    scroller = runtime.window.profile_scroll
    assert scroller.viewport().height() >= scroller.widget().minimumSizeHint().height()
    assert runtime.window.auto_toggle.text() == i18n.t("automatic_mode")


@pytest.mark.parametrize("enabled,opacity,alpha", [(True, 140 / 255, 140), (False, 0.25, 255), (True, 0, 0), (True, 0.25, 64), (True, 1, 255)])
def test_monitor_background_rendering_includes_scroll_and_msi_containers(runtime, qapp, enabled, opacity, alpha):
    runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=enabled, background_opacity=opacity, integrations=(replace(runtime.controller.settings.integrations[0], enabled=True),)))
    window = runtime.window
    window.show()
    qapp.processEvents()
    assert window.windowOpacity() == 1
    image = window.grab().toImage()
    for widget, point in ((window.shell, QPoint(4, window.shell.height() // 2)), (window.flow, QPoint(2, 2)), (window.msi_section, QPoint(2, 2)), (window.profile_scroll.widget(), QPoint(2, 2))):
        position = widget.mapTo(window, point)
        assert image.pixelColor(position).alpha() == alpha, widget.objectName()
    assert image.pixelColor(0, 0).alpha() == 0
    title = window.title.grab().toImage()
    assert max(title.pixelColor(x, y).alpha() for x in range(title.width()) for y in range(title.height())) == 255
    dialog = SettingsWindow(runtime.controller, window)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.grab().toImage().pixelColor(4, 4).alpha() == 255
    finally:
        dialog.reject()
        dialog.deleteLater()


@pytest.mark.parametrize("enabled", [False, True])
def test_transparency_draft_save_reset_and_restart(runtime, qapp, enabled):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        assert dialog.transparency.text() == "Transparent background"
        dialog.transparency.setChecked(not enabled)
        assert runtime.controller.settings.transparency is True
        dialog.load_draft()
        assert dialog.transparency.isChecked()
        dialog.transparency.setChecked(enabled)
        dialog.save()
        assert runtime.controller.store.load().transparency is enabled
        restarted_controller = Controller(runtime.controller.store, NoCredentials())
        restarted = window_module.MonitorWindow(restarted_controller, lambda: None)
        try:
            restarted.show()
            qapp.processEvents()
            assert restarted.grab().toImage().pixelColor(4, restarted.height() // 2).alpha() == (140 if enabled else 255)
        finally:
            restarted._quitting = True
            restarted.close()
            restarted_controller.stop()
            restarted.deleteLater()
        runtime.window.show()
        runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=True))
        qapp.processEvents()
        assert runtime.window.grab().toImage().pixelColor(4, runtime.window.height() // 2).alpha() == 140
    finally:
        dialog.deleteLater()


def test_background_transparency_draft_reset_save_and_cancel(runtime, qapp):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        dialog.background_transparency.setValue(75)
        assert runtime.controller.settings.background_opacity == 140 / 255
        dialog.load_draft()
        assert dialog.gather().background_opacity == 140 / 255
        dialog.transparency.setChecked(False)
        assert not dialog.background_transparency.isEnabled()
        dialog.transparency.setChecked(True)
        dialog.background_transparency.setValue(75)
        dialog.save()
        assert runtime.controller.store.load().background_opacity == 0.25
        runtime.window.show()
        qapp.processEvents()
        assert runtime.window.grab().toImage().pixelColor(4, runtime.window.height() // 2).alpha() == 64
    finally:
        dialog.deleteLater()
    cancelled = SettingsWindow(runtime.controller, runtime.window)
    cancelled.background_transparency.setValue(10)
    cancelled.reject()
    assert runtime.controller.store.load().background_opacity == 0.25
    cancelled.deleteLater()


@pytest.mark.parametrize("visible", [False, True])
def test_startup_preserves_visibility_without_temporary_show(runtime, visible):
    events = []

    class VisibilityTracker(QObject):
        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
                events.append(event.type())
            return False

    tracker = VisibilityTracker()
    runtime.window.installEventFilter(tracker)
    runtime.window._desired_visible = visible
    runtime.window.startup()
    assert events == ([QEvent.Type.Show] if visible else [])
    assert runtime.window.isVisible() == visible
    for _ in range(3):
        runtime.window.request_visibility(True)
        assert runtime.window.isVisible()
        runtime.window.request_visibility(False)
        assert not runtime.window.isVisible()


def test_rapid_visibility_requests_and_transparency_changes_use_latest_request(runtime, qapp):
    window = runtime.window
    window.startup()
    for enabled in (False, True, False, True):
        runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=enabled))
        window.request_visibility(True)
        assert window.isVisible()
        window.request_visibility(False)
    qapp.processEvents()
    assert window.isHidden()
    assert runtime.controller.store.load().monitor_visible is False
    window.request_visibility(True)
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == 140


@pytest.mark.parametrize("visible", [False, True])
def test_shutdown_prevents_later_window_show(runtime, visible):
    window = runtime.window
    window._desired_visible = visible
    window.startup()
    runtime.stop()
    window.startup()
    window.request_visibility(True)
    QTest.qWait(250)
    assert window.isHidden()
    assert not window._layout_timer.isActive()


@pytest.mark.parametrize("enabled,alpha", [(True, 140), (False, 255)])
def test_background_survives_repeated_show_and_native_handle_recreation(runtime, qapp, enabled, alpha):
    runtime.controller.apply_settings(replace(runtime.controller.settings, transparency=enabled))
    window = runtime.window
    window.show()
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == alpha
    window.hide()
    window.destroy()
    window.show()
    qapp.processEvents()
    assert window.grab().toImage().pixelColor(4, window.height() // 2).alpha() == alpha

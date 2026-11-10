from dataclasses import replace
import time

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox
import pytest

from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, METRICS
from ecoflow_monitor.ui.settings_window import SettingsWindow


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    instance.stop()
    instance.window.deleteLater()
    instance.mini.deleteLater()
    qapp.processEvents()


def metric_mode(dialog, key):
    return dialog.metrics.rows[key].mode


def test_hidden_metrics_roundtrip_and_existing_settings():
    settings = replace(AppSettings(), mini_metrics=("output_power",),
                       mini_hidden_metrics=("estimated_runtime", "battery_level"))
    assert AppSettings.from_dict(settings.to_dict()) == settings
    data = settings.to_dict()
    del data["mini_widget"]["hidden_metrics"]
    legacy = AppSettings.from_dict(data)
    assert legacy.mini_metrics == ("output_power",)
    assert legacy.mini_hidden_metrics == ()
    hidden_only = replace(settings, mini_metrics=())
    assert AppSettings.from_dict(hidden_only.to_dict()) == hidden_only


@pytest.mark.parametrize("visible,hidden", [
    ((), ()), (("battery_level",), ("battery_level",)),
    ((), ("input_power", "input_power")), ((), ("unknown",)),
])
def test_hidden_metric_groups_reject_invalid_choices(visible, hidden):
    with pytest.raises(ValueError):
        replace(AppSettings(), mini_metrics=visible, mini_hidden_metrics=hidden).validate()
    data = AppSettings().to_dict()
    data["mini_widget"].update(metrics=list(visible), hidden_metrics=list(hidden))
    with pytest.raises(ValueError):
        AppSettings.from_dict(data)


def test_metric_modes_keep_drafts_and_merge_other_preferences(runtime, qapp):
    controller = runtime.controller
    dialog = SettingsWindow(controller, runtime.window)
    try:
        assert metric_mode(dialog, "battery_level").currentData() == "always"
        assert metric_mode(dialog, "estimated_runtime").currentData() == "off"
        metric_mode(dialog, "estimated_runtime").setCurrentIndex(1)
        metric_mode(dialog, "output_power").setCurrentIndex(2)
        assert controller.settings.mini_hidden_metrics == ()
        dialog.integrations_section.set_expanded(True)
        dialog.load_draft()
        assert dialog.integrations_section.header.isChecked()
        assert metric_mode(dialog, "estimated_runtime").currentData() == "off"
        metric_mode(dialog, "estimated_runtime").setCurrentIndex(1)
        metric_mode(dialog, "output_power").setCurrentIndex(2)
        controller.update_preferences(mini_dock="right")
        draft = dialog.gather()
        assert draft.mini_dock == "right"
        assert draft.mini_metrics == ("battery_level", "input_power")
        assert draft.mini_hidden_metrics == ("estimated_runtime",)
        dialog.reject()
        assert controller.settings.mini_hidden_metrics == ()
        dialog.save()
        stored = controller.store.load()
        assert stored.mini_metrics == draft.mini_metrics
        assert stored.mini_hidden_metrics == draft.mini_hidden_metrics
        dialog.load_draft()
        assert metric_mode(dialog, "estimated_runtime").currentData() == "expanded"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_mini_expansion_updates_values_and_resets_on_reopen(runtime, qapp):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, mini_enabled=True,
                                      mini_metrics=("battery_level",),
                                      mini_hidden_metrics=("output_power", "estimated_runtime")))
    mini = runtime.mini
    assert tuple(mini.labels) == ("battery_level",)
    assert mini.reveal_button.isVisible()
    mini.reveal_button.setFocus()
    QTest.keyClick(mini.reveal_button, Qt.Key.Key_Space)
    qapp.processEvents()
    assert tuple(mini.labels) == ("battery_level", "output_power", "estimated_runtime")
    assert mini.overlay.preferences.position_locked is False
    controller.telemetry = Telemetry(10, 240, 70, time.time(), time.monotonic())
    controller.connection = ConnectionState.LIVE
    controller.changed.emit()
    assert mini.labels["output_power"].text() == "240 W"
    assert mini.labels["output_power"].isEnabled()
    controller.connection = ConnectionState.STALE
    controller.changed.emit()
    assert mini.labels["output_power"].isEnabled()
    assert "Data is out of date" in mini.status.text()
    assert mini.reveal_button.isChecked()
    controller.update_preferences(mini_enabled=False)
    controller.update_preferences(mini_enabled=True)
    assert not mini.reveal_button.isChecked()
    assert tuple(mini.labels) == ("battery_level",)
    controller.update_preferences(mini_hidden_metrics=())
    assert mini.reveal_button.isHidden()


def test_metric_reordering_keeps_modes_and_group_order(runtime, qapp):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        metric_mode(dialog, "output_power").setCurrentIndex(1)
        metric_mode(dialog, "estimated_runtime").setCurrentIndex(1)
        assert dialog.metrics.move_before("estimated_runtime", "output_power")
        assert dialog.metrics.move_before("input_power", "battery_level")
        qapp.processEvents()
        assert metric_mode(dialog, "estimated_runtime").currentData() == "expanded"
        assert metric_mode(dialog, "output_power").currentData() == "expanded"
        draft = dialog.gather()
        assert draft.mini_metrics == ("input_power", "battery_level")
        assert draft.mini_hidden_metrics == ("estimated_runtime", "output_power")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("dock", ["", "left", "right", "top", "bottom"])
@pytest.mark.parametrize("hidden_only", [False, True])
def test_mini_groups_fit_work_area_in_both_states(runtime, qapp, language, dock, hidden_only):
    mini = runtime.mini
    for scale in (0.5, 1, 3):
        runtime.controller.apply_settings(replace(runtime.controller.settings, language=language,
                                                 mini_enabled=True, mini_dock=dock, mini_scale=scale,
                                                 mini_metrics=() if hidden_only else METRICS[:3],
                                                 mini_hidden_metrics=METRICS if hidden_only else METRICS[3:]))
        for expanded in (False, True, False):
            mini.reveal_button.setChecked(expanded)
            qapp.processEvents()
            expected = (() if hidden_only else METRICS[:3]) + (mini.controller.settings.mini_hidden_metrics if expanded else ())
            assert tuple(mini.labels) == expected
            assert mini.screen().availableGeometry().contains(mini.geometry())
            assert mini.rect().contains(mini.reveal_button.geometry())
            assert mini.status.isVisible()
            for label in mini.labels.values():
                tile = label.parentWidget()
                assert mini.body.rect().contains(tile.geometry())
                assert label.height() >= label.fontMetrics().height()


def test_monitor_disclosure_retains_selection_and_resets_on_hide(runtime, qapp):
    controller = runtime.controller
    first = replace(controller.settings.integrations[0], enabled=True)
    second = replace(first, id="second", label="Second")
    controller.apply_settings(replace(controller.settings, integrations=(first, second)))
    window = runtime.window
    window.show()
    assert window.monitoring.msi_section.isVisible()
    assert not window.monitoring.integration_selector.isVisible()
    QTest.keyClick(window.monitoring.msi_section.header, Qt.Key.Key_Space)
    qapp.processEvents()
    assert window.monitoring.integration_selector.isVisible()
    window.monitoring.integration_selector.setCurrentIndex(1)
    window.monitoring.profile_choice.setCurrentIndex(2)
    window.monitoring.msi_section.set_expanded(False)
    controller.changed.emit()
    window.monitoring.msi_section.set_expanded(True)
    assert window.monitoring.integration_selector.currentData() == second.id
    assert window.monitoring.profile_choice.currentIndex() == 2
    window.hide()
    window.show()
    assert not window.monitoring.integration_selector.isVisible()
    controller.apply_settings(replace(controller.settings, integrations=()))
    assert window.monitoring.msi_section.isHidden()


def test_settings_integrations_keep_draft_and_allow_empty_modules(runtime, qapp, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.integrations.confirm", lambda *args: True)
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        dialog.show()
        dialog.tabs.setCurrentIndex(1)
        assert dialog.integrations_section.header.isHidden()
        assert dialog.integrations_editor.isVisible()
        dialog.integrations_section.set_expanded(True)
        editor = dialog.integrations_editor
        editor.label.setText("Draft module")
        dialog.integrations_section.set_expanded(False)
        dialog.integrations_section.set_expanded(True)
        assert editor.label.text() == "Draft module"
        dialog.tabs.setCurrentIndex(2)
        dialog.tabs.setCurrentIndex(1)
        assert editor.isVisible()
        dialog.hide()
        dialog.show()
        assert editor.isVisible()
        dialog.integrations_section.set_expanded(True)
        assert editor.label.text() == "Draft module"
        editor.remove()
        assert editor.empty_hint.isVisible()
        editor.add()
        assert editor.body.isVisible()
        assert len(editor.modules()) == 1
    finally:
        dialog.deleteLater()
        qapp.processEvents()

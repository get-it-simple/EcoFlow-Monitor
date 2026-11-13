from dataclasses import replace
import time

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox
import pytest
from qt_helpers import close_runtime

from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, METRICS
from ecoflow_monitor.ui.settings_window import SettingsWindow


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    close_runtime(instance, qapp)


def test_hidden_metrics_roundtrip_and_existing_settings():
    settings = replace(AppSettings(), mini_metrics=("output_power",), mini_hidden_metrics=("estimated_runtime", "battery_level"))
    assert AppSettings.from_dict(settings.to_dict()) == settings
    data = settings.to_dict()
    data["version"] = 3
    del data["mini_widget"]["hidden_metrics"]
    legacy = AppSettings.from_dict(data)
    assert legacy.mini_metrics == ("output_power",)
    assert legacy.mini_hidden_metrics == ()
    hidden_only = replace(settings, widget=replace(settings.widget, small_metrics=(), large_metrics=(), expanded_metrics=("estimated_runtime",)))
    assert AppSettings.from_dict(hidden_only.to_dict()) == hidden_only


@pytest.mark.parametrize("visible,hidden", [
    ((), ()), (("battery_level",), ("battery_level",)),
    ((), ("input_power", "input_power")), ((), ("unknown",)),
])
def test_hidden_metric_groups_reject_invalid_choices(visible, hidden):
    with pytest.raises(ValueError):
        replace(AppSettings(), widget=replace(AppSettings().widget, small_metrics=visible, large_metrics=visible, expanded_metrics=hidden)).validate()
    data = AppSettings().to_dict()
    data["mini_widget"].update(metrics=list(visible), hidden_metrics=list(hidden))
    with pytest.raises(ValueError):
        AppSettings.from_dict(data)


def test_metric_modes_keep_drafts_and_merge_other_preferences(runtime, qapp):
    controller = runtime.controller
    dialog = SettingsWindow(controller, runtime.window)
    try:
        assert dialog.metrics.modes["battery_level"] == "small"
        assert dialog.metrics.modes["estimated_runtime"] == "large"
        dialog.metrics.set_mode("estimated_runtime", "expanded")
        dialog.metrics.set_mode("output_power", "off")
        assert controller.settings.widget.expanded_metrics == ()
        dialog.integrations_section.set_expanded(True)
        dialog.load_draft()
        assert dialog.integrations_section.header.isChecked()
        assert dialog.metrics.modes["estimated_runtime"] == "large"
        dialog.metrics.set_mode("estimated_runtime", "expanded")
        dialog.metrics.set_mode("output_power", "off")
        controller.update_preferences(widget=replace(controller.settings.widget, dock="right"))
        draft = dialog.gather()
        assert draft.widget.dock == "right"
        assert draft.widget.small_metrics == ("battery_level", "input_power")
        assert draft.widget.expanded_metrics == ("estimated_runtime",)
        dialog.reject()
        assert controller.settings.widget.expanded_metrics == ()
        dialog.save()
        stored = controller.store.load()
        assert stored.widget.small_metrics == draft.widget.small_metrics
        assert stored.widget.expanded_metrics == draft.widget.expanded_metrics
        dialog.load_draft()
        assert dialog.metrics.modes["estimated_runtime"] == "expanded"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_mini_expansion_updates_values_and_resets_on_reopen(runtime, qapp):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, widget=replace(controller.settings.widget, enabled=True, small_metrics=("battery_level",), large_metrics=("battery_level",), expanded_metrics=("output_power", "estimated_runtime"))))
    mini = runtime.mini
    assert tuple(mini.labels) == ("battery_level",)
    mini.setFocus()
    qapp.processEvents()
    assert mini.reveal_button.isEnabled()
    mini.reveal_button.setFocus()
    QTest.keyClick(mini.reveal_button, Qt.Key.Key_Space)
    qapp.processEvents()
    assert tuple(mini.labels) == ("battery_level",)
    assert mini.details.isVisible()
    assert mini.composition.details == ("output_power", "estimated_runtime")
    assert mini.overlay.preferences.position_locked is False
    controller.telemetry = Telemetry(10, 240, 70, time.time(), time.monotonic())
    controller.connection = ConnectionState.LIVE
    controller.changed.emit()
    assert mini.details.readings["output_power"].value.text() == "240 W"
    assert mini.details.readings["output_power"].isEnabled()
    controller.connection = ConnectionState.STALE
    controller.changed.emit()
    assert mini.details.readings["output_power"].isEnabled()
    assert "Data is out of date" in mini.status_indicator.toolTip()
    assert mini.reveal_button.isChecked()
    controller.update_preferences(widget=replace(controller.settings.widget, enabled=False))
    controller.update_preferences(widget=replace(controller.settings.widget, enabled=True))
    assert not mini.reveal_button.isChecked()
    assert tuple(mini.labels) == ("battery_level",)
    controller.update_preferences(widget=replace(controller.settings.widget, expanded_metrics=()))
    assert not mini.reveal_button.isEnabled()


def test_metric_reordering_keeps_modes_and_group_order(runtime, qapp):
    dialog = SettingsWindow(runtime.controller, runtime.window)
    try:
        dialog.metrics.set_mode("output_power", "expanded")
        dialog.metrics.set_mode("estimated_runtime", "expanded")
        assert dialog.metrics.move_before("estimated_runtime", "output_power")
        assert dialog.metrics.move_before("input_power", "battery_level")
        qapp.processEvents()
        assert dialog.metrics.modes["estimated_runtime"] == "expanded"
        assert dialog.metrics.modes["output_power"] == "expanded"
        draft = dialog.gather()
        assert draft.widget.small_metrics == ("input_power", "battery_level")
        assert draft.widget.expanded_metrics == ("estimated_runtime", "output_power")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("dock", ["", "left", "right", "top", "bottom"])
@pytest.mark.parametrize("hidden_only", [False, True])
@pytest.mark.design
def test_mini_groups_fit_work_area_in_both_states(runtime, qapp, language, dock, hidden_only):
    mini = runtime.mini
    for scale in (0.5, 1, 3):
        runtime.controller.apply_settings(replace(runtime.controller.settings, widget=replace(runtime.controller.settings.widget, enabled=True, dock=dock, scale=scale, small_metrics=() if hidden_only else METRICS[:3], large_metrics=() if hidden_only else METRICS[:3], expanded_metrics=METRICS if hidden_only else METRICS[3:]), language=language))
        for expanded in (False, True, False):
            mini.reveal_button.setChecked(expanded)
            qapp.processEvents()
            assert tuple(mini.labels) == mini.composition.visible
            assert set(mini.composition.visible) | set(mini.composition.details) == set(mini.controller.settings.widget.large_metrics) | set(mini.controller.settings.widget.expanded_metrics)
            assert mini.details.isVisible() == expanded
            assert mini.screen().availableGeometry().contains(mini.geometry())
            assert mini.rect().contains(mini.reveal_button.geometry())
            assert mini.status.isHidden()
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
    from ecoflow_monitor.ui.integration_creation import IntegrationCreationDialog

    def create(dialog):
        dialog.label.setText("New integration")
        dialog.save()
        return dialog.result()

    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(IntegrationCreationDialog, QDialog.exec.__name__, create)
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

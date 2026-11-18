from dataclasses import replace
import time

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QFontMetricsF, QScreen
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QBoxLayout
import pytest
from qt_helpers import close_runtime

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.ui.components import SettingRow


@pytest.fixture
def runtime(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 4096, 3000))
    monkeypatch.setattr(QScreen, "geometry", lambda self: QRect(0, 0, 4096, 3000))
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    close_runtime(instance, qapp)
    i18n.setup("en")


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_settings_rows_fit_without_stretching_and_hotkeys_align(runtime, qapp, language, scale):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language=language, scale=scale))
    window = runtime.dashboard
    window.show()
    for width, height in ((1085, 1206), (1048, 1206), (800, 600), (1500, 900)):
        window.resize(round(width * scale), round(height * scale))
        window.navigation.setCurrentRow(3)
        window.integrations_editor.body.setCurrentIndex(0)
        QTest.qWait(40)
        for row in window.integrations_editor.body.currentWidget().findChildren(SettingRow):
            assert row.rect().contains(row.control.geometry())
            assert row.height() <= row.heightForWidth(row.width()) + round(8 * scale)
            if row.row.direction() == QBoxLayout.Direction.TopToBottom:
                assert 0 <= row.control.y() - row.label.geometry().bottom() <= round(12 * scale)
        window.navigation.setCurrentRow(4)
        qapp.processEvents()
        for field, editor, enabled in window.hotkey_rows.values():
            if field.isVisibleTo(window):
                assert abs(editor.geometry().center().y() - enabled.geometry().center().y()) <= 1
                assert field.rect().contains(editor.geometry())
                assert field.rect().contains(enabled.geometry())
        assert window.tabs.currentWidget().horizontalScrollBar().maximum() == 0


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_monitoring_cards_have_padding_and_output_groups_reflow(runtime, qapp, language, scale):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language=language, scale=scale))
    window = runtime.dashboard
    window.show()
    page = window.monitoring
    counts = []
    for width in (800, 1500):
        window.resize(round(width * scale), round(900 * scale))
        QTest.qWait(40)
        for card in page.cards:
            reading = card.findChild(type(page.readings["input_power"]))
            assert reading.x() >= round(16 * scale)
            assert card.width() - reading.geometry().right() >= round(16 * scale)
        sections = list(page.port_sections.values())
        for card, title, grid, note in sections:
            assert abs(card.width() - page.balance_card.width()) <= 1
            assert page.rect().contains(card.geometry())
            for index in range(grid.count()):
                assert card.body.rect().contains(grid.itemAt(index).widget().geometry())
        ac = page.port_values["ac_output"][0]
        dc = page.port_values["dc_output"][0]
        assert ac.parentWidget() is page.port_sections["ac"][0].body
        assert dc.parentWidget() is page.port_sections["other"][0].body
        grid = page.port_sections["other"][2]
        counts.append(max(grid.getItemPosition(index)[1] for index in range(grid.count())) + 1)
    assert counts[0] == 1 and counts[1] == 3


@pytest.mark.design
@pytest.mark.parametrize("opacity", [0, 0.82, 1])
@pytest.mark.parametrize("language", ["en", "uk"])
def test_widget_values_grow_to_available_space_and_keep_text_inside(runtime, qapp, opacity, language):
    controller = runtime.controller
    controller.apply_settings(replace(controller.settings, language=language))
    controller.telemetry = Telemetry(640, 185, 72, time.time(), controller.clock())
    controller.connection = ConnectionState.LIVE
    controller.update_widget(enabled=True, opacity=opacity, small_metrics=("input_power",), large_metrics=("input_power",))
    widget = runtime.widget
    reading = widget.readings["input_power"]
    sizes = []
    for width, height in ((320, 88), (900, 400), (320, 88)):
        widget.resize(width, height)
        QTest.qWait(40)
        reading.fit_value()
        sizes.append(reading.value.font().pixelSize())
        rect = reading.value.contentsRect().adjusted(2, 2, -2, -2)
        bounds = QFontMetricsF(reading.value.font()).boundingRect(QRectF(0, 0, rect.width(), 1000000), Qt.TextFlag.TextSingleLine, reading.value.text())
        assert bounds.width() <= rect.width() and bounds.height() <= rect.height()
        assert reading.rect().contains(reading.value.geometry())
    assert sizes[1] > max(56, sizes[0])
    assert sizes[2] == sizes[0]
    composition = widget.composition
    for value in (0, 99999, None):
        controller.telemetry = replace(controller.telemetry, input_w=value)
        controller.changed.emit()
        qapp.processEvents()
        assert widget.composition == composition
        assert reading.rect().contains(reading.value.geometry())

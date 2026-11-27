from dataclasses import replace
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QEnterEvent, QScreen
from PySide6.QtTest import QTest

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry, present_remaining_time
from ecoflow_monitor.polling import dependencies
from ecoflow_monitor.ui.common import metric_text
from ecoflow_monitor.ui.notifications import current_notices
from qt_helpers import close_runtime


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    close_runtime(instance, qapp)


@pytest.mark.parametrize("soc,fraction", [(0, 0), (50, 0.5), (100, 1)])
def test_mains_presentation_uses_charge_fraction_without_numeric_infinity(soc, fraction):
    presentation = present_remaining_time(None, True, 500, True, soc, True)
    assert presentation.mode == "infinity" and presentation.hours is None
    assert presentation.charge_fraction == fraction


@pytest.mark.parametrize("ac_fresh,soc,soc_fresh", [(False, 50, True), (True, None, True), (True, float("nan"), True), (True, 50, False)])
def test_mains_without_fresh_charge_data_waits(ac_fresh, soc, soc_fresh):
    assert present_remaining_time(None, True, 500, ac_fresh, soc, soc_fresh).mode == "waiting"


def test_mains_indicator_is_shared_and_stale_data_reverts_to_loader(runtime, qapp):
    controller = runtime.controller
    controller.connection = ConnectionState.LIVE
    controller.telemetry = Telemetry(700, 200, 80, time.time(), controller.clock(), 20, 80, ports=(("ac_input", 700),))
    controller.changed.emit()
    readings = (runtime.dashboard.monitoring.readings["estimated_runtime"], runtime.widget.readings["estimated_runtime"], runtime.widget.details.readings["estimated_runtime"])
    for reading in readings:
        reading.refresh()
        assert not reading.infinity.isHidden()
        assert reading.loader.isHidden()
        assert "∞" in reading.accessibleName()
    assert metric_text(controller, "estimated_runtime") == "∞"
    assert controller.remaining_time is None
    assert "ac_input" in dependencies("estimated_runtime")
    controller.connection = ConnectionState.STALE
    for reading in readings:
        reading.refresh()
        assert reading.infinity.isHidden()
        assert not reading.loader.isHidden()
    assert metric_text(controller, "estimated_runtime") == "—"


def test_solar_keeps_charge_time_and_missing_estimate_uses_spinner(runtime):
    controller = runtime.controller
    controller.connection = ConnectionState.LIVE
    controller.telemetry = Telemetry(700, 200, 50, time.time(), controller.clock(), 20, 80, ports=(("ac_input", 0), ("pv_input", 700)))
    assert controller.remaining_presentation.mode == "charge"
    controller.telemetry = replace(controller.telemetry, output_w=None)
    reading = runtime.dashboard.monitoring.readings["estimated_runtime"]
    reading.refresh()
    assert not reading.loader.isHidden() and reading.infinity.isHidden()


def test_collapse_preserves_settings_draft_and_live_summary(runtime, qapp):
    dashboard = runtime.dashboard
    dashboard.capacity.setValue(3000)
    dashboard._draft_changed()
    page = dashboard.monitoring
    page.port_sections["ac"][0].toggle.click()
    page.port_sections["other"][0].toggle.click()
    assert runtime.controller.settings.ac_collapsed and runtime.controller.settings.other_collapsed
    for group in ("ac", "other"):
        card = page.port_sections[group][0]
        assert card.body.isHidden() and not card.summary.isHidden()
    dashboard.save()
    assert runtime.controller.settings.capacity_wh == 3000
    assert runtime.controller.settings.ac_collapsed and runtime.controller.settings.other_collapsed
    assert runtime.controller.store.load().ac_collapsed


@pytest.mark.design
@pytest.mark.parametrize("width", [800, 1300])
@pytest.mark.parametrize("collapsed", [False, True])
def test_output_disclosure_hover_preserves_geometry(runtime, qapp, monkeypatch, width, collapsed):
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 1600, 1000))
    monkeypatch.setattr(QScreen, "geometry", lambda self: QRect(0, 0, 1600, 1000))
    controller = runtime.controller
    controller.update_preferences(ac_collapsed=collapsed, other_collapsed=collapsed)
    dashboard = runtime.dashboard
    dashboard.show()
    dashboard.resize(width, 580)
    page = dashboard.monitoring
    scroll = dashboard.tabs.currentWidget()
    viewport = scroll.viewport()
    cards = [page.port_sections[group][0] for group in ("ac", "other")]
    qapp.processEvents()
    page.reflow()
    QTest.qWait(35)
    assert dashboard.width() == width
    assert (cards[0].y() == cards[1].y()) == (collapsed and width == 1300)

    def geometry():
        return (
            dashboard.geometry(), viewport.geometry(), page.geometry(),
            tuple((card.geometry(), card.toggle.geometry()) for card in cards),
            scroll.verticalScrollBar().value(), scroll.verticalScrollBar().maximum(),
        )

    for card in cards:
        button = card.toggle
        button.clearFocus()
        qapp.sendEvent(card, QEvent(QEvent.Type.Leave))
        page.refresh()
        QTest.qWait(35)
        scroll.ensureWidgetVisible(button)
        initial = geometry()
        region = QRect(button.mapTo(viewport, button.rect().topLeft()), button.size())
        assert viewport.rect().contains(region)
        hidden = viewport.grab(region).toImage()
        assert hidden == viewport.grab(region.translated(-region.width(), 0)).toImage()

        local = button.mapTo(card, button.rect().center())
        enter = QEnterEvent(QPointF(local), QPointF(card.mapTo(dashboard, local)), QPointF(card.mapToGlobal(local)))
        qapp.sendEvent(card, enter)
        QTest.qWait(35)
        assert geometry() == initial
        assert viewport.grab(region).toImage() != hidden
        assert button.arrowType() == (Qt.ArrowType.DownArrow if collapsed else Qt.ArrowType.UpArrow)

        qapp.sendEvent(card, QEvent(QEvent.Type.Leave))
        QTest.qWait(35)
        assert geometry() == initial
        assert viewport.grab(region).toImage() == hidden

        button.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.qWait(35)
        assert geometry() == initial
        assert viewport.grab(region).toImage() != hidden
        button.clearFocus()
        QTest.qWait(35)
        assert geometry() == initial
        assert viewport.grab(region).toImage() == hidden

    cards[0].toggle.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(cards[0].toggle, Qt.Key.Key_Space)
    assert controller.settings.ac_collapsed is not collapsed
    assert controller.settings.other_collapsed is collapsed


def test_notifications_empty_live_updates_and_target_preserve_drafts(runtime, qapp):
    controller = runtime.controller
    controller.settings = replace(controller.settings, integrations=())
    controller.notice = ""
    indicator = runtime.dashboard.monitoring.notifications
    indicator.refresh(controller)
    indicator.click()
    dialog = indicator.dialog
    assert dialog.isVisible() and dialog.entries == ()
    controller.notice = "credential_error"
    indicator.refresh(controller)
    assert len(dialog.entries) == 1 and dialog.entries[0].target == "connection_service"
    runtime.dashboard.capacity.setValue(3000)
    dialog.buttons["general"].click()
    assert not dialog.isVisible()
    assert runtime.dashboard.tabs.currentIndex() == 1
    assert runtime.dashboard.connection_tabs.currentIndex() == 1
    assert runtime.dashboard.capacity.value() == 3000
    controller.notice = ""
    indicator.refresh(controller)
    assert not current_notices(controller)
    indicator.click()
    assert indicator.dialog is dialog
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()


def test_notification_selects_integration_by_stable_id(runtime):
    modules = runtime.controller.settings.integrations
    runtime.controller.settings = replace(runtime.controller.settings, integrations=(replace(modules[0], enabled=True), *modules[1:]))
    runtime.dashboard.load_draft()
    indicator = runtime.dashboard.monitoring.notifications
    indicator.refresh(runtime.controller)
    module = next(module for module in runtime.controller.settings.integrations if module.enabled)
    indicator.click()
    indicator.dialog.buttons["integration:" + module.id].click()
    assert runtime.dashboard.page_keys[runtime.dashboard.tabs.currentIndex()] == "integrations"
    assert runtime.dashboard.integrations_editor.selector.currentData() == module.id


def test_empty_notifications_follow_live_language_changes(runtime):
    from PySide6.QtWidgets import QLabel
    controller = runtime.controller
    controller.settings = replace(controller.settings, integrations=())
    controller.notice = ""
    indicator = runtime.dashboard.monitoring.notifications
    indicator.refresh(controller)
    indicator.click()
    for language in ("en", "uk"):
        i18n.setup(language)
        indicator.refresh(controller)
        assert any(label.text() == i18n.t("notifications_empty") and not label.isHidden() for label in indicator.dialog.findChildren(QLabel))


def test_fresh_mains_stays_static_when_other_required_sensors_are_stale(runtime, qapp):
    from ecoflow_monitor.polling import CORE, TelemetryPatch
    controller = runtime.controller
    controller.connection = ConnectionState.STALE
    controller.telemetry = Telemetry(700, 200, 50, time.time(), controller.clock(), 20, 80, ports=(("ac_input", 700),))
    controller.polling.set_demand(CORE | {"ac_input"}, CORE, controller.clock())
    controller.polling.complete(TelemetryPatch(("ac_input", "battery_level"), (("ac_input", 700), ("battery_level", 50))), time.time(), controller.clock())
    runtime.dashboard.show()
    controller.changed.emit()
    reading = runtime.dashboard.monitoring.readings["estimated_runtime"]
    reading.refresh()
    assert controller.remaining_presentation.mode == "infinity"
    assert not reading.infinity.isHidden()
    assert not reading.infinity.motion_active()


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_compact_ports_and_notification_geometry(runtime, qapp, language, scale):
    from PySide6.QtWidgets import QFrame, QVBoxLayout
    from ecoflow_monitor.ui.monitoring import MonitoringPage
    from ecoflow_monitor.ui.common import scaled_font
    from ecoflow_monitor.ui.theme import settings_style
    i18n.setup(language)
    controller = runtime.controller
    controller.settings = replace(controller.settings, language=language, scale=scale, ac_collapsed=True)
    dashboard = runtime.dashboard
    dashboard.apply_appearance()
    controller.notice = "startup_action_required"
    controller.connection = ConnectionState.LIVE
    controller.telemetry = Telemetry(700, 200, 50, time.time(), controller.clock(), 20, 80,
                                    ports=(("ac_input", 700), ("pv_input", 0), ("ac_output", 150), ("dc_output", 20),
                                           ("usb_c1", 15), ("usb_c2", 5), ("usb_c3", 0), ("usb_a1", 5), ("usb_a2", 5), ("anderson", 0)),
                                    ac_enabled=True, dc_enabled=True)
    frame = QFrame()
    frame.setObjectName("dashboard")
    frame._scale = scale
    frame.setStyleSheet(settings_style(scale=scale))
    scaled_font(frame, scale)
    box = QVBoxLayout(frame)
    box.setContentsMargins(0, 0, 0, 0)
    page = MonitoringPage(controller, lambda: None, frame)
    box.addWidget(page)
    frame.resize(round(1200 * scale), round(1000 * scale))
    frame.show()
    page.reflow()
    qapp.processEvents()
    ac, other = page.port_sections["ac"][0], page.port_sections["other"][0]
    assert ac.body.isHidden()
    assert not other.body.isHidden()
    assert page._outputs_joined
    assert ac.y() == other.y() and ac.x() < other.x()
    assert abs(ac.width() - other.width()) <= 2
    ac.toggle.setFocus()
    assert ac.toggle.arrowType() == Qt.ArrowType.DownArrow
    previews = Path("build/update-previews")
    previews.mkdir(parents=True, exist_ok=True)
    assert frame.grab().save(str(previews / f"ports-wide-{language}-{scale}.png"))
    frame.resize(round(600 * scale), round(1000 * scale))
    page.reflow()
    qapp.processEvents()
    assert not page._outputs_joined
    assert ac.y() < other.y()
    assert frame.grab().save(str(previews / f"ports-narrow-{language}-{scale}.png"))
    page.notifications.click()
    dialog = page.notifications.dialog
    qapp.processEvents()
    assert dialog.content_scroll.isVisible()
    assert dialog.width() <= dialog.screen().availableGeometry().width()
    for button in dialog.buttons.values():
        assert button.width() > 0 and button.height() > 0
    assert dialog.grab().save(str(previews / f"notifications-{language}-{scale}.png"))
    controller.connection = ConnectionState.LIVE
    controller.telemetry = Telemetry(700, 200, 80, time.time(), controller.clock(), 20, 80, ports=(("ac_input", 700),))
    page.refresh()
    qapp.processEvents()
    reading = page.readings["estimated_runtime"]
    assert not reading.infinity.isHidden()
    assert reading.infinity.geometry().size() == reading.value.size()
    assert reading.grab().save(str(previews / f"infinity-{language}-{scale}.png"))
    dialog.close()
    frame.close()

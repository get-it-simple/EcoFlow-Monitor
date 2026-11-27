import math
from dataclasses import replace

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLayout, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import METRICS
from ecoflow_monitor.widget_layout import Rectangle, details_placement, widget_layout
from ecoflow_monitor.ui.common import ScreenTracker, effective_scale, scaled_font, status_color, status_tooltip
from ecoflow_monitor.ui.components import StatusBadge
from ecoflow_monitor.ui.focus import FocusBinding
from ecoflow_monitor.ui.motion import MotionSurface
from ecoflow_monitor.ui.overlay import OverlayControls, configure_overlay_button
from ecoflow_monitor.ui.readings import EnergyBalance, MetricReading
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.resize import WindowResize
from ecoflow_monitor.ui.scrollbar import install_scrollbars
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.tokens import RADIUS, REFLOW_INTERVAL_MS, TOKENS, surface
from ecoflow_monitor.ui.typography import ValueLabel


WIDGET = TOKENS["widget"]


class DetailsPanel(QFrame):
    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("widget_details")
        self.owner = owner
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.shell = QFrame(self)
        self.shell.setObjectName("details_shell")
        outer.addWidget(self.shell)
        layout = QVBoxLayout(self.shell)
        self.scroll = QScrollArea(self.shell)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        install_scrollbars(self)
        layout.addWidget(self.scroll)
        self.body = QFrame()
        self.grid = QGridLayout(self.body)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body)
        self.readings = {key: MetricReading(owner.controller, key, self.body) for key in METRICS}
        for reading in self.readings.values():
            reading.box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            reading.setMinimumSize(0, 0)
            reading.hide()
        self._focus_binding = FocusBinding(self)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.owner.reveal_button.setChecked(False)
            self.owner.reveal_button.setFocus(Qt.FocusReason.OtherFocusReason)
            event.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.owner.reveal_button.setChecked(False)


class AdaptiveWidget(QFrame):
    composition_changed = Signal()

    def __init__(self, controller, open_dashboard):
        super().__init__()
        self.controller = controller
        self.open_dashboard = open_dashboard
        self._initial = True
        self._stopping = False
        self._laying_out = False
        self._has_been_shown = False
        self._signature = None
        self._hovered = False
        self._keyboard_focus = False
        self._sleep_geometry = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setObjectName("adaptive_widget")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.shell = MotionSurface(controller, None, self)
        self.shell.setObjectName("widget_shell")
        outer.addWidget(self.shell)
        self.title = ValueLabel(self.shell)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.status = StatusBadge(controller, self.shell)
        self.status.compact = True
        self.status_indicator = ValueLabel(self.shell)
        self.status_indicator.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.status_indicator.setProperty("focus_target", True)
        self.status_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.readings = {key: MetricReading(controller, key, self.shell) for key in METRICS}
        self.body = self.shell
        self.battery = self.readings["battery_level"]
        self.runtime = self.readings["estimated_runtime"]
        self.progress = EnergyBalance(controller, self.battery, battery=True)
        self.progress.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.progress.hide()
        for reading in self.readings.values():
            reading.box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            reading.setMinimumSize(0, 0)
            reading.hide()
        self.toolbar = QFrame(self)
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.overlay = OverlayControls(self, controller, "widget")
        self.reveal_button = QToolButton(self.toolbar)
        self.reveal_button.setCheckable(True)
        self.reveal_button.setIcon(QIcon(icon_path("expand")))
        self.reveal_button.toggled.connect(self._toggle_details)
        self.close_button = QToolButton(self.toolbar)
        self.close_button.setIcon(QIcon(icon_path("close")))
        self.close_button.clicked.connect(lambda: controller.update_widget(enabled=False))
        for button in (self.overlay.pin, self.reveal_button, self.overlay.menu_button, self.close_button):
            row.addWidget(button)
        self.overlay.menu.aboutToShow.connect(self._menu_opened)
        self.overlay.menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self, self._toolbar_visibility))
        self.toolbar.hide()
        self.details = DetailsPanel(self)
        self.details.hide()
        self._focus_binding = FocusBinding(self)
        self._resize = WindowResize(self)
        self._screen_tracker = ScreenTracker(self, self.refresh)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(500)
        self._geometry_timer.timeout.connect(self._save_geometry)
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(REFLOW_INTERVAL_MS)
        self._reflow_timer.timeout.connect(self._resize_layout)
        self._surface_timer = QTimer(self, singleShot=True)
        self._surface_timer.timeout.connect(self._sync_sleep_surface)
        QApplication.instance().installEventFilter(self)
        QApplication.instance().focusChanged.connect(self._toolbar_visibility)
        controller.changed.connect(self.refresh)
        controller.system_activity_changed.connect(self._system_activity_changed)

    def _system_activity_changed(self, activity):
        if self._stopping:
            return
        if activity.sleeping and self._sleep_geometry is None:
            self._sleep_geometry = self.geometry()
        if self._sleep_geometry is not None:
            self._surface_timer.start(0)

    def _sync_sleep_surface(self):
        if self._stopping or self._sleep_geometry is None:
            return
        self._geometry_timer.stop()
        self._reflow_timer.stop()
        self.overlay.menu.hide()
        self.hide()
        self.details.hide()
        if self.controller.system_activity.paused:
            return
        self.details.destroy()
        self.destroy()
        geometry, self._sleep_geometry = self._sleep_geometry, None
        if not self._initial:
            self.setGeometry(geometry)
        self.refresh()

    def _area(self):
        screen = self.screen() or QApplication.primaryScreen()
        return screen.geometry() if self.controller.ui_settings.widget.above_taskbar else screen.availableGeometry()

    def refresh(self):
        if self._laying_out or self._stopping:
            return
        preferences = self.controller.ui_settings.widget
        if self._sleep_geometry is not None:
            if not preferences.enabled:
                self.reveal_button.setChecked(False)
            return
        self.overlay.apply()
        area = self._area()
        minimal_allowed = bool(set(preferences.small_metrics) & set(METRICS[:3]))
        self.scale = min(effective_scale(self, preferences.scale), area.width() / WIDGET["minimumWidth" if minimal_allowed else "smallWidth"], area.height() / WIDGET["minimumHeight" if minimal_allowed else "smallHeight"])
        scale = self.scale
        self.setMinimumSize(round(WIDGET["minimumWidth" if minimal_allowed else "smallWidth"] * scale), round(WIDGET["minimumHeight" if minimal_allowed else "smallHeight"] * scale))
        self.setMaximumSize(area.width(), min(area.height(), round(WIDGET["maximumHeight"] * scale)))
        if self._initial:
            context = preferences.overlay
            screen = next((item for item in QApplication.screens() if item.name() == context.screen), self.screen())
            if screen is not None:
                self.setScreen(screen)
            x, y, width, height = preferences.geometry
            self.resize(width, height)
            self.move(area.x() + 20 if (x, y) == (-1, -1) else x, area.y() + 20 if (x, y) == (-1, -1) else y)
            self._initial = False
        self._apply_style(preferences)
        self._reflow()
        self.setWindowTitle(t("adaptive_widget"))
        self.title.setText(self.controller.device_name)
        self.status.refresh()
        self.status.outlined = preferences.opacity == 0
        self.status_indicator.setText("!")
        self.status_indicator.setToolTip(status_tooltip(self.controller))
        self.status_indicator.setAccessibleName(self.status_indicator.toolTip())
        self.status_indicator.setStyleSheet(f"color: {status_color(self.controller)}; font-size: {round(12 * scale)}px;")
        for label in (self.title, self.status_indicator):
            label.outlined = preferences.opacity == 0
        self.reveal_button.setToolTip(t("hide_hidden_metrics" if self.reveal_button.isChecked() else "show_hidden_metrics"))
        self.reveal_button.setAccessibleName(self.reveal_button.toolTip())
        self.close_button.setToolTip(t("hide"))
        self.close_button.setAccessibleName(t("hide"))
        self.setVisible(preferences.enabled)
        if self.isVisible():
            self._has_been_shown = True
            self._place_core()
        if not preferences.enabled:
            self.reveal_button.setChecked(False)
        elif self.reveal_button.isChecked():
            self._toggle_details(True)
        self._toolbar_visibility()

    def _apply_style(self, preferences):
        signature = (self.scale, preferences.opacity)
        if signature == self._signature:
            return
        self._signature = signature
        scale = self.scale
        scaled_font(self, scale)
        material = "Panel" if preferences.opacity == 1 else "Clear" if preferences.opacity == 0 else "Overlay"
        self.shell.material = material
        self.shell.motion_radius = RADIUS["window"] * scale
        self.setStyleSheet(settings_style(scale=scale) + f"#widget_shell, #details_shell {{ background: {surface(preferences.opacity)}; border: 1px solid {surface(preferences.opacity, 'border')}; border-radius: {round(RADIUS['window'] * scale)}px; }}")
        self.details.setStyleSheet(self.styleSheet())
        for button in (self.overlay.pin, self.overlay.menu_button, self.reveal_button, self.close_button):
            configure_overlay_button(button, scale)
            button.setStyleSheet(button.styleSheet() + f"QToolButton {{ background: {surface(WIDGET['toolbarAlpha'])}; }} QToolButton:hover {{ background: {surface(1, 'raised')}; }} QToolButton:pressed {{ background: {surface(1, 'canvas')}; }} QToolButton:checked {{ border-color: {surface(1, 'accent')}; }}")
        self._resize.scale = scale

    def _reflow(self):
        if self._laying_out or not hasattr(self, "scale"):
            return
        preferences = self.controller.ui_settings.widget
        signature = (self.size(), self.scale, replace(preferences, geometry=(0, 0, 0, 0)),
                     self.controller.ui_settings.language, self.controller.fresh)
        if signature == getattr(self, "_layout_signature", None):
            for key in self.composition.visible:
                self.readings[key].refresh()
            for key in self.composition.details:
                self.details.readings[key].refresh()
            self.progress.refresh()
            return
        self._layout_signature = signature
        self._laying_out = True
        try:
            preferences = self.controller.ui_settings.widget
            scale = self.scale
            gap = round(WIDGET["gap"] * scale)
            inset = round(WIDGET["outerPadding"] * scale)
            width, height = self.width() / scale, self.height() / scale
            probe = widget_layout(preferences, width, height, 1, 1, WIDGET)
            minimal = probe.mode == "minimal"
            metadata_height = 0 if minimal else round(24 * scale) * (preferences.show_device_name + preferences.show_connection_status)
            self.title.setVisible(not minimal and preferences.show_device_name)
            self.status.setVisible(not minimal and preferences.show_connection_status)
            y = inset
            for label in (self.title, self.status):
                if not label.isHidden():
                    label.setGeometry(inset, y, max(1, self.width() - 2 * inset), round(24 * scale))
                    y += round(24 * scale)
            padding = WIDGET["smallPadding" if probe.wide else probe.mode + "Padding"]
            value_size = WIDGET["minimalValueSize"] if minimal else WIDGET["valueSize"]
            labels = preferences.labels_visible and not minimal
            available_width = max(1, self.width() - 2 * inset)
            cell_width = round(WIDGET["minimalCellWidth" if minimal else "cellWidth" if probe.mode == "large" else "smallCellWidth"] * scale)
            columns = max(1, (available_width + gap) // (cell_width + gap))
            candidates = preferences.large_metrics if probe.mode == "large" else preferences.small_metrics
            if minimal:
                candidates = tuple(key for key in candidates if key in METRICS[:3])
            columns = min(columns, max(1, len(candidates)))
            target_width = max(1, (available_width - gap * (columns - 1)) // columns)
            card_height = 1
            compact_label_width = max(1, target_width - round(2 * padding * scale)) if probe.mode != "large" or probe.wide else None
            for key, reading in self.readings.items():
                reading.material = self.shell.material
                reading.configure(scale, value_size, preferences.opacity == 0, labels, preferences.values_on_hover and not minimal, padding, 0, compact_label_width, fit=True)
                if key not in candidates:
                    continue
                card_height = max(card_height, reading.baseline_size(target_width).height())
            usable_height = max(0, self.height() - 2 * inset - metadata_height)
            rows = max(0, (usable_height + gap) // (card_height + gap))
            self.composition = widget_layout(preferences, width, height, columns, rows, WIDGET)
            self.labels = {key: self.readings[key].value for key in self.composition.visible}
            for key, reading in self.readings.items():
                reading.setVisible(key in self.composition.visible)
            visible_count = len(self.composition.visible)
            used_rows = math.ceil(visible_count / columns) if visible_count else 0
            row_height = card_height if not used_rows else max(card_height, (usable_height - gap * (used_rows - 1)) // used_rows)
            for index, key in enumerate(self.composition.visible):
                reading = self.readings[key]
                reading.setGeometry(inset + index % columns * (target_width + gap), y + index // columns * (row_height + gap), target_width, row_height)
                if key == "battery_level" and probe.mode == "large" and not probe.wide and row_height >= round(124 * scale):
                    reading.configure(scale, WIDGET["heroValueSize"], preferences.opacity == 0, labels, preferences.values_on_hover, padding, 0, fit=True, reserved_bottom=16)
                    if reading.baseline_size(target_width).width() > target_width:
                        reading.configure(scale, value_size, preferences.opacity == 0, labels, preferences.values_on_hover, padding, 0, fit=True, reserved_bottom=16)
                reading.fit_value()
            self.progress.configure(scale, self.shell.material)
            self.progress.setGeometry(round(padding * scale), self.battery.height() - round((padding + 12) * scale), max(1, self.battery.width() - round(2 * padding * scale)), round(12 * scale))
            self.progress.setVisible("battery_level" in self.composition.visible and probe.mode == "large" and not probe.wide and row_height >= round(124 * scale))
            self.reveal_button.setEnabled(bool(self.composition.details))
            if not self.composition.details:
                self.reveal_button.setChecked(False)
            button_size = round(36 * scale)
            self.toolbar.setGeometry(max(inset, self.width() - inset - button_size * 4), inset, button_size * 4, button_size)
            self.toolbar.layout().activate()
            self.status_indicator.setVisible(not preferences.show_connection_status and not self.controller.fresh or minimal and not self.controller.fresh)
            indicator_inset = max(TOKENS["focus"]["offset"] + TOKENS["focus"]["width"], round(WIDGET["minimalPadding"] * scale))
            self.status_indicator.setGeometry(indicator_inset, indicator_inset, round(16 * scale), round(16 * scale))
            focusable = [self.overlay.pin, self.reveal_button, self.overlay.menu_button, self.close_button]
            if not self.status_indicator.isHidden():
                focusable.append(self.status_indicator)
            focusable.extend(self.readings[key] for key in self.composition.visible)
            for previous, following in zip([self, *focusable], focusable):
                self.setTabOrder(previous, following)
            self.status_indicator.raise_()
            self.toolbar.raise_()
            self._resize.layout()
            self._update_details()
        finally:
            self._laying_out = False
        signature = (self.composition.visible, self.composition.details)
        if signature != getattr(self, "_composition_signature", None):
            self._composition_signature = signature
            self.composition_changed.emit()

    def _update_details(self):
        if not hasattr(self, "composition"):
            return
        panel = self.details
        changed = getattr(self, "_details_keys", None) != self.composition.details
        if changed:
            self._details_keys = self.composition.details
            while panel.grid.count():
                panel.grid.takeAt(0).widget().hide()
        scale = self.scale
        preferences = self.controller.ui_settings.widget
        for index, key in enumerate(self.composition.details):
            reading = panel.readings[key]
            reading.material = self.shell.material
            reading.configure(scale, WIDGET["valueSize"], preferences.opacity == 0, preferences.labels_visible, False, WIDGET["largePadding"], 0, fit=True)
            if changed:
                panel.grid.addWidget(reading, index, 0)
                reading.show()
        panel.grid.setContentsMargins(0, 0, 0, 0)
        panel.grid.setSpacing(round(WIDGET["gap"] * scale))
        for first, second in zip(self.composition.details, self.composition.details[1:]):
            panel.setTabOrder(panel.readings[first], panel.readings[second])
        self._details_height = sum(panel.readings[key].sizeHint().height() for key in self.composition.details) + round((WIDGET["gap"] * max(0, len(self.composition.details) - 1) + 24) * scale)
        self._place_details()

    def _place_details(self):
        if not hasattr(self, "scale"):
            return
        area = self._area()
        core = self.geometry()
        height = min(round(WIDGET["detailsHeight"] * self.scale), max(round(WIDGET["smallHeight"] * self.scale), getattr(self, "_details_height", 0)))
        direction, rect = details_placement(Rectangle(core.x(), core.y(), core.width(), core.height()), Rectangle(area.x(), area.y(), area.width(), area.height()), (round(WIDGET["detailsWidth"] * self.scale), height), self.controller.ui_settings.widget.dock, round(WIDGET["gap"] * self.scale))
        self.details.direction = direction
        self.details.setGeometry(rect.x, rect.y, rect.width, rect.height)

    def _place_core(self):
        area = self._area()
        x = max(area.left(), min(self.x(), area.right() - self.width() + 1))
        y = max(area.top(), min(self.y(), area.bottom() - self.height() + 1))
        dock = self.controller.ui_settings.widget.dock
        x = area.left() if dock == "left" else area.right() - self.width() + 1 if dock == "right" else x
        y = area.top() if dock == "top" else area.bottom() - self.height() + 1 if dock == "bottom" else y
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        self._place_details()

    def _toggle_details(self, enabled):
        self._update_details()
        flags = self.details.windowFlags()
        for flag in (Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowTransparentForInput):
            flags = flags | flag if self.windowFlags() & flag else flags & ~flag
        if flags != self.details.windowFlags():
            self.details.setWindowFlags(flags)
        self.details.setVisible(enabled and self.isVisible())
        self._toolbar_visibility()

    def _menu_opened(self):
        self.overlay.menu.addSeparator()
        self.overlay.menu.addAction(t("open_dashboard"), self.open_dashboard)
        self.toolbar.show()

    def _toolbar_visibility(self, *args):
        focus = QApplication.focusWidget()
        focused = self._keyboard_focus and focus is not None and focus.window() in (self, self.details)
        interactive = not self.windowFlags() & Qt.WindowType.WindowTransparentForInput
        self.toolbar.setVisible(self.isVisible() and interactive and (self._hovered or self.underMouse() or self.details.underMouse() or focused or self.overlay.menu.isVisible()))

    def eventFilter(self, watched, event):
        if isinstance(watched, QWidget) and watched.window() in (self, self.details):
            if event.type() == QEvent.Type.KeyPress:
                self._keyboard_focus = True
                self._focus_binding.keyboard = True
                self.details._focus_binding.keyboard = True
                QTimer.singleShot(0, self, self._toolbar_visibility)
            elif event.type() == QEvent.Type.FocusIn:
                if event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
                    self._keyboard_focus = True
                elif event.reason() in (Qt.FocusReason.MouseFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
                    self._keyboard_focus = False
                QTimer.singleShot(0, self, self._toolbar_visibility)
            elif event.type() == QEvent.Type.MouseButtonPress:
                self._keyboard_focus = False
        if watched is self and event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            self._hovered = event.type() == QEvent.Type.Enter
            QTimer.singleShot(0, self, self._toolbar_visibility)
        elif watched is self.details and event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            QTimer.singleShot(0, self, self._toolbar_visibility)
        if event.type() == QEvent.Type.MouseButtonPress and watched in (self.shell, self.title, *self.readings.values(), *(label for reading in self.readings.values() for label in (reading.label, reading.value))):
            if event.button() == Qt.MouseButton.LeftButton and not self.property("position_locked") and self.windowHandle() is not None:
                self.windowHandle().startSystemMove()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.reveal_button.isChecked():
            self.reveal_button.setChecked(False)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _save_geometry(self):
        if self._sleep_geometry is not None or self.controller.preview is not None or self._initial or not self._has_been_shown or self.isMinimized() or self.isMaximized():
            return
        preferences = self.controller.settings.widget
        screen = self.screen()
        context = replace(preferences.overlay, screen=screen.name(), dpi=screen.logicalDotsPerInch()) if screen else preferences.overlay
        geometry = (self.x(), self.y(), self.width(), self.height())
        if geometry != preferences.geometry or context != preferences.overlay:
            self.controller.update_widget(geometry=geometry, overlay=context)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_geometry_timer") and not self._initial and self._sleep_geometry is None:
            self._place_details()
            self._geometry_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_geometry_timer") and not self._initial and self._sleep_geometry is None:
            if not self._reflow_timer.isActive():
                self._reflow_timer.start()
            self._geometry_timer.start()

    def _resize_layout(self):
        if not self._stopping:
            self._reflow()
            self._place_core()

    def showEvent(self, event):
        super().showEvent(event)
        self._screen_tracker.watch()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._hovered = False
        if hasattr(self, "details"):
            if self._sleep_geometry is None and not self.property("applying_overlay"):
                self.reveal_button.setChecked(False)
            self.details.hide()
            self.toolbar.hide()

    def closeEvent(self, event):
        if not self.controller._stopped:
            event.ignore()
            self.controller.update_widget(enabled=False)
        else:
            super().closeEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "_signature"):
            self._signature = None
            QTimer.singleShot(0, self, self.refresh)
        return super().event(event)

    def stop(self):
        self._stopping = True
        self._surface_timer.stop()
        self._reflow_timer.stop()
        self._geometry_timer.stop()
        self._save_geometry()
        self._screen_tracker.stop()
        self._focus_binding.stop()
        self.details._focus_binding.stop()
        QApplication.instance().focusChanged.disconnect(self._toolbar_visibility)
        QApplication.instance().removeEventFilter(self)
        self.details.hide()
        self.hide()

import math
from dataclasses import replace

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLayout, QToolButton, QVBoxLayout

from ecoflow_monitor.adapters.windows import raise_without_focus
from ecoflow_monitor.i18n import get_current_language, t
from ecoflow_monitor.ui.common import ScreenTracker, clamp_window, effective_scale, scaled_font, status_tooltip
from ecoflow_monitor.ui.common import metric_text, status_color, status_text
from ecoflow_monitor.ui.overlay import OverlayControls, configure_overlay_button
from ecoflow_monitor.ui.readings import MetricReading
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.tokens import RADIUS, surface
from ecoflow_monitor.ui.focus import FocusBinding
from ecoflow_monitor.ui.motion import MotionHeader, MotionSurface


class MiniWidget(QFrame):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("mini")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.shell = MotionSurface(controller, "battery", self)
        self.shell.setObjectName("mini_shell")
        self._layout.addWidget(self.shell)
        self.content = QVBoxLayout(self.shell)
        self.status = MotionHeader(controller, "DELTA 3 Max", self.shell)
        header = QHBoxLayout()
        header.addWidget(self.status, 1)
        self.overlay = OverlayControls(self, controller, "mini")
        header.addWidget(self.overlay.pin)
        self.reveal_button = QToolButton(self)
        self.reveal_button.setIcon(QIcon(icon_path("expand")))
        self.reveal_button.setCheckable(True)
        self.reveal_button.toggled.connect(self.refresh)
        header.addWidget(self.reveal_button)
        header.addWidget(self.overlay.menu_button)
        self.close_button = QToolButton(self)
        self.close_button.setIcon(QIcon(icon_path("close")))
        self.close_button.clicked.connect(lambda: controller.update_preferences(mini_enabled=False))
        header.addWidget(self.close_button)
        self.content.addLayout(header)
        self.body = QFrame(self)
        self.grid = QGridLayout(self.body)
        self.grid.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.content.addWidget(self.body)
        self.labels = {}
        self.readings = {}
        self._signature = None
        self._fit_context = None
        self._fit_limit = 3
        self._screen_tracker = ScreenTracker(self, self.refresh)
        self._initial = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_position)
        controller.changed.connect(self.refresh)
        self._focus_binding = FocusBinding(self)

    def refresh(self):
        settings = self.controller.ui_settings
        self.overlay.apply()
        if not settings.mini_hidden_metrics:
            self.reveal_button.blockSignals(True)
            self.reveal_button.setChecked(False)
            self.reveal_button.blockSignals(False)
        expanded = self.reveal_button.isChecked()
        self.reveal_button.setVisible(bool(settings.mini_hidden_metrics))
        self.reveal_button.setToolTip(t("hide_hidden_metrics" if expanded else "show_hidden_metrics"))
        self.reveal_button.setAccessibleName(self.reveal_button.toolTip())
        self.close_button.setToolTip(t("hide"))
        self.close_button.setAccessibleName(t("hide"))
        groups = tuple(group for group in (settings.mini_metrics, settings.mini_hidden_metrics if expanded else ()) if group)
        largest = max((len(group) for group in groups), default=0)
        scale = effective_scale(self, settings.mini_scale)
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.geometry() if settings.mini_above_taskbar else screen.availableGeometry()
        context = (settings.mini_scale, area.width(), area.height(), groups, get_current_language())
        if context != self._fit_context:
            self._fit_context = context
            self._fit_limit = 3
        scale = min(scale, self._fit_limit)
        opacity = 1 - settings.mini_transparency
        material = "Panel" if opacity == 1 else "Clear" if opacity == 0 else "Overlay"
        self.shell.material = material
        count = max(1, min(3, largest, int((area.width() / scale - 32) / 112)))
        rows = sum(math.ceil(len(group) / count) for group in groups)
        scale *= min(1, area.width() / (max(320, count * 136 + 32) * scale), area.height() / ((rows * 72 + 60) * scale))
        self.shell.motion_radius = RADIUS["card"] * scale
        self.status.configure(scale)
        lengths = tuple(len(metric_text(self.controller, key)) for group in groups for key in group)
        signature = (groups, scale, settings.mini_transparency, get_current_language(), settings.mini_dock, area.width(), area.height(), lengths)
        if signature != self._signature:
            self._signature = signature
            while self.grid.count():
                item = self.grid.takeAt(0)
                item.widget().hide()
                item.widget().deleteLater()
            self.labels = {}
            self.readings = {}
            scaled_font(self, scale)
            opacity = 1 - settings.mini_transparency
            self.setStyleSheet(settings_style(scale=scale) + f"#mini_shell {{ background: {surface(opacity)}; border: 1px solid {surface(opacity, 'border')}; border-radius: {round(RADIUS['card'] * scale)}px; }}")
            self.content.setContentsMargins(round(16 * scale), round(8 * scale), round(16 * scale), round(8 * scale))
            self.content.setSpacing(round(4 * scale))
            self.grid.setSpacing(round(8 * scale))
            for button in (self.overlay.pin, self.overlay.menu_button, self.reveal_button, self.close_button):
                configure_overlay_button(button, scale)
            offset = 0
            for group in groups:
                for index, key in enumerate(group):
                    reading = MetricReading(self.controller, key, self.body)
                    reading.material = material
                    reading.configure(scale, 24, opacity == 0)
                    self.readings[key] = reading
                    self.labels[key] = reading.value
                    self.grid.addWidget(reading, offset + index // count, index % count)
                    reading.show()
                offset += math.ceil(len(group) / count)
            self.body.setVisible(bool(groups))
            self.setMinimumSize(0, 0)
            self.content.activate()
            self.grid.activate()
            minimum = self.minimumSizeHint().expandedTo(QSize(round(320 * scale), round(88 * scale)))
            fit = min(1, area.width() / max(1, minimum.width()), area.height() / max(1, minimum.height()))
            if fit < 1 and scale > 0.1:
                self._fit_limit = scale * fit * 0.98
                self._signature = None
                return self.refresh()
            self.setMinimumSize(minimum)
            self.adjustSize()
            self._dock()
        for reading in self.readings.values():
            reading.refresh()
        self.status.setText("DELTA 3 Max · " + status_text(self.controller))
        self.status.setToolTip(status_tooltip(self.controller))
        self.status.setAccessibleName(self.status.toolTip())
        self.status.setStyleSheet(f"color: {status_color(self.controller)};")
        self.status.outlined = settings.mini_transparency == 1
        self.status.setWordWrap(True)
        self.status.setMaximumWidth(max(80, round(190 * scale)))
        self.adjustSize()
        if self._initial:
            self._initial = False
            context = settings.mini_overlay
            target = next((item for item in QApplication.screens() if item.name() == context.screen), screen)
            if context.screen:
                self.setScreen(target)
            x, y = settings.mini_position
            if x != -1 or y != -1:
                self.move(x, y)
            else:
                self.move(area.right() - self.width() - 20, area.top() + 20)
        self._dock()
        if self.isVisible() != settings.mini_enabled:
            self.setVisible(settings.mini_enabled)
        if self.isVisible():
            clamp_window(self, settings.mini_above_taskbar)

    def _dock(self):
        settings = self.controller.settings
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.geometry() if settings.mini_above_taskbar else screen.availableGeometry()
        if settings.mini_dock == "left":
            self.move(area.left(), self.y())
        elif settings.mini_dock == "right":
            self.move(area.right() - self.width() + 1, self.y())
        elif settings.mini_dock == "top":
            self.move(self.x(), area.top())
        elif settings.mini_dock == "bottom":
            self.move(self.x(), area.bottom() - self.height() + 1)
        clamp_window(self, settings.mini_above_taskbar)

    def _save_position(self):
        if self.controller.preview is not None:
            return
        position = (self.x(), self.y())
        screen = self.screen()
        old = self.controller.settings.mini_overlay
        context = replace(old, screen=screen.name(), dpi=screen.logicalDotsPerInch()) if screen else old
        if position != self.controller.settings.mini_position or context != old:
            self.controller.update_preferences(mini_position=position, mini_overlay=context)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_initial") and not self._initial:
            self._save_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_initial") and not self._initial:
            self._dock()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.property("applying_overlay"):
            self.reveal_button.setChecked(False)
        self._screen_tracker.watch()
        raise_without_focus(self)

    def closeEvent(self, event):
        if not self.controller._stopped:
            event.ignore()
            self.controller.update_preferences(mini_enabled=False)
        else:
            self._screen_tracker.stop()
            super().closeEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "_signature"):
            self._signature = None
            QTimer.singleShot(0, self.refresh)
        return super().event(event)

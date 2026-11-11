from dataclasses import replace

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLayout, QToolButton, QVBoxLayout

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import DragHeader, ScreenTracker, clamp_window, effective_scale, metric_text, scaled_font
from ecoflow_monitor.ui.components import StatusBadge
from ecoflow_monitor.ui.focus import FocusBinding
from ecoflow_monitor.ui.motion import MotionSurface
from ecoflow_monitor.ui.overlay import OverlayControls, configure_overlay_button
from ecoflow_monitor.ui.readings import EnergyBalance, MetricReading
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.resize import WindowResize
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.tokens import RADIUS, surface
from ecoflow_monitor.ui.typography import ValueLabel


class MainWidget(QFrame):
    def __init__(self, controller, open_dashboard):
        super().__init__()
        self.controller = controller
        self._initial = True
        self._has_been_shown = False
        self._signature = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("main_widget")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.shell = MotionSurface(controller, "battery", self)
        self.shell.setObjectName("main_shell")
        outer.addWidget(self.shell)
        self.content = QVBoxLayout(self.shell)
        self.content.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        header = QHBoxLayout()
        self.title = DragHeader("DELTA 3 Max", self.shell)
        header.addWidget(self.title, 1)
        self.overlay = OverlayControls(self, controller, "main")
        header.addWidget(self.overlay.pin)
        self.open_button = QToolButton(self.shell)
        self.open_button.setIcon(QIcon(icon_path("expand")))
        self.open_button.clicked.connect(lambda: open_dashboard(0))
        header.addWidget(self.open_button)
        header.addWidget(self.overlay.menu_button)
        self.close_button = QToolButton(self.shell)
        self.close_button.setIcon(QIcon(icon_path("close")))
        self.close_button.clicked.connect(lambda: controller.update_preferences(main_enabled=False))
        header.addWidget(self.close_button)
        self.content.addLayout(header)
        self.battery = MetricReading(controller, "battery_level", self.shell)
        self.battery.channel = None
        self.battery.box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content.addWidget(self.battery)
        self.runtime = MetricReading(controller, "estimated_runtime", self.shell)
        self.content.addWidget(self.runtime)
        self.content.addStretch(1)
        self.progress = EnergyBalance(controller, self.shell, battery=True)
        self.content.addWidget(self.progress)
        readings = QHBoxLayout()
        self.readings = {}
        for key in ("input_power", "output_power"):
            reading = MetricReading(controller, key, self.shell)
            self.readings[key] = reading
            readings.addWidget(reading, 1)
        self.content.addLayout(readings)
        footer = QHBoxLayout()
        self.status = StatusBadge(controller, self.shell)
        self.status.compact = True
        self.limits = ValueLabel(self.shell)
        self.limits.setWordWrap(True)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.limits, 1)
        self.content.addLayout(footer)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(500)
        self._geometry_timer.timeout.connect(self._save_geometry)
        self._screen_tracker = ScreenTracker(self, self.refresh)
        self._focus_binding = FocusBinding(self)
        self._resize = WindowResize(self)
        controller.changed.connect(self.refresh)

    def refresh(self):
        settings = self.controller.ui_settings
        self.overlay.apply()
        scale = effective_scale(self, settings.main_scale)
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        scale = min(scale, area.width() / 360, area.height() / 320)
        opacity = settings.background_opacity if settings.transparency else 1
        material = "Panel" if opacity == 1 else "Clear" if opacity == 0 else "Overlay"
        signature = (scale, opacity)
        if signature != self._signature:
            self._signature = signature
            scaled_font(self, scale)
            self.setStyleSheet(settings_style(scale=scale) + f"#main_shell {{ background: {surface(opacity)}; border: 1px solid {surface(opacity, 'border')}; border-radius: {round(RADIUS['window'] * scale)}px; }}")
            self.content.setContentsMargins(round(20 * scale), round(8 * scale), round(20 * scale), round(8 * scale))
            self.content.setSpacing(round(8 * scale))
            self.setMinimumSize(round(360 * scale), round(320 * scale))
            for button in (self.overlay.pin, self.overlay.menu_button, self.open_button, self.close_button):
                configure_overlay_button(button, scale)
            if not self._initial:
                self.resize(self.size().expandedTo(self.minimumSize()).boundedTo(area.size()))
        self.shell.material = material
        self.shell.motion_radius = RADIUS["window"] * scale
        self.progress.configure(scale, material)
        self.title.outlined = opacity == 0
        self.limits.outlined = opacity == 0
        self.status.outlined = opacity == 0
        self.battery.configure(scale, 56, opacity == 0, settings.monitor_labels_visible, settings.monitor_values_on_hover)
        self.runtime.configure(scale, 16, opacity == 0, settings.monitor_labels_visible, settings.monitor_values_on_hover)
        for reading in self.readings.values():
            reading.material = material
            reading.configure(scale, 24, opacity == 0, settings.monitor_labels_visible, settings.monitor_values_on_hover)
        self.progress.refresh()
        self.status.refresh()
        self.limits.setText(t("widget_limits").format(minimum=metric_text(self.controller, "minimum_discharge_level"), maximum=metric_text(self.controller, "maximum_charge_level")))
        self.open_button.setToolTip(t("open_dashboard"))
        self.open_button.setAccessibleName(self.open_button.toolTip())
        self.close_button.setToolTip(t("hide"))
        self.close_button.setAccessibleName(t("hide"))
        self.setWindowTitle(t("main_widget"))
        self._resize.scale = scale
        self._resize.layout()
        if self._initial:
            self._initial = False
            context = settings.main_overlay
            target = next((item for item in QApplication.screens() if item.name() == context.screen), screen)
            if context.screen:
                self.setScreen(target)
            x, y, width, height = settings.monitor_geometry
            if (x, y) == (-1, -1):
                self.resize(round(width * scale), round(height * scale))
                self.move(area.left() + 20, area.top() + 20)
            else:
                self.resize(width, height)
                self.move(x, y)
        self.setVisible(settings.main_enabled)
        if self.isVisible():
            self._has_been_shown = True
            clamp_window(self)

    def _save_geometry(self):
        if self.controller.preview is not None or self._initial or not self._has_been_shown or self.isMinimized() or self.isMaximized():
            return
        geometry = (self.x(), self.y(), self.width(), self.height())
        screen = self.screen()
        old = self.controller.settings.main_overlay
        context = replace(old, screen=screen.name(), dpi=screen.logicalDotsPerInch()) if screen else old
        if geometry != self.controller.settings.monitor_geometry or context != old:
            self.controller.update_preferences(monitor_geometry=geometry, main_overlay=context)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_geometry_timer") and not self._initial:
            self._geometry_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_geometry_timer") and not self._initial:
            self._geometry_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._screen_tracker.watch()

    def closeEvent(self, event):
        if not self.controller._stopped:
            event.ignore()
            self.controller.update_preferences(main_enabled=False)
        else:
            self._screen_tracker.stop()
            super().closeEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "_signature"):
            self._signature = None
            QTimer.singleShot(0, self, self.refresh)
        return super().event(event)

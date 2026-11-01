from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QLabel, QVBoxLayout

from ecoflow_monitor.adapters.windows import raise_without_focus
from ecoflow_monitor.i18n import get_current_language, t
from ecoflow_monitor.ui.common import clamp_window, effective_scale, metric_text, scaled_font, status_tooltip


class MiniWidget(QFrame):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("mini")
        self._layout = QVBoxLayout(self)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self._layout.addWidget(self.status)
        self.body = QFrame(self)
        self.grid = QGridLayout(self.body)
        self._layout.addWidget(self.body)
        self.labels = {}
        self._signature = None
        self._drag = None
        self._initial = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_position)
        controller.changed.connect(self.refresh)

    def refresh(self):
        settings = self.controller.settings
        scale = effective_scale(self, settings.mini_scale)
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.geometry() if settings.mini_above_taskbar else screen.availableGeometry()
        vertical = settings.mini_dock in {"left", "right"}
        signature = (settings.mini_metrics, scale, settings.mini_transparency, settings.mini_dock, settings.mini_above_taskbar, get_current_language(), area.width(), area.height())
        if signature != self._signature:
            self._signature = signature
            while self.grid.count():
                item = self.grid.takeAt(0)
                widget = item.widget()
                widget.hide()
                widget.deleteLater()
            self.labels = {}
            scaled_font(self, scale)
            self._layout.setContentsMargins(round(8 * scale), round(6 * scale), round(8 * scale), round(6 * scale))
            self._layout.setSpacing(round(4 * scale))
            self.grid.setContentsMargins(0, 0, 0, 0)
            self.grid.setSpacing(round(4 * scale))
            self.status.setMinimumHeight(round(19 * scale))
            self.status.setStyleSheet(f"font-size: {round(11 * scale)}px;")
            count = max(1, int((area.height() - 70 * scale) / (74 * scale))) if vertical else max(1, int((area.width() - 20) / (154 * scale)))
            for index, key in enumerate(settings.mini_metrics):
                tile = QFrame(self.body)
                tile.setObjectName("mini_tile")
                tile.setFixedSize(round(148 * scale), round(70 * scale))
                box = QVBoxLayout(tile)
                label = QLabel(t(key), tile)
                label.setWordWrap(True)
                label.setStyleSheet(f"font-size: {round(11 * scale)}px;")
                value = QLabel(tile)
                value.setObjectName("mini_value")
                box.addWidget(label)
                box.addWidget(value)
                self.labels[key] = value
                row, column = (index % count, index // count) if vertical else (index // count, index % count)
                self.grid.addWidget(tile, row, column)
            alpha = round(255 * (1 - settings.mini_transparency))
            self.setStyleSheet(f"#mini {{ background: rgba(18, 28, 31, {alpha}); border: 1px solid #456057; border-radius: 10px; }} #mini_tile {{ background: rgba(30, 48, 47, {alpha}); border-radius: 6px; }} QLabel {{ color: #eef8f4; background: transparent; }} #mini_value {{ font-size: {round(20 * scale)}px; font-weight: 600; }}")
            self.adjustSize()
            self._dock()
        for key, label in self.labels.items():
            label.setText(metric_text(self.controller, key))
            label.setEnabled(self.controller.fresh)
        self.status.setText(("● " if self.controller.fresh else "○ ") + t(self.controller.connection.value))
        self.status.setToolTip(status_tooltip(self.controller))
        self.status.setMaximumWidth(max(round(148 * scale), self.body.sizeHint().width()))
        self.adjustSize()
        if self._initial:
            self._initial = False
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
        position = (self.x(), self.y())
        if position != self.controller.settings.mini_position:
            self.controller.update_preferences(mini_position=position)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.controller.update_preferences(mini_position=(self.x(), self.y()), mini_dock="")
        super().mouseReleaseEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        if not self._initial:
            self._save_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        raise_without_focus(self)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "_signature"):
            self._signature = None
            QTimer.singleShot(0, self.refresh)
        return super().event(event)

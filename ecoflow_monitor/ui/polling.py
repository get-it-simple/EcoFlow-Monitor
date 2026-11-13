from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.polling import DASHBOARD_SENSORS
from ecoflow_monitor.ui.motion import motion_driver
from ecoflow_monitor.ui.tokens import COLORS, TOKENS


class SensorDemand(QObject):
    def __init__(self, controller, dashboard, widget):
        super().__init__(dashboard)
        self.controller, self.dashboard, self.widget = controller, dashboard, widget
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        for view in (dashboard, widget, widget.details):
            view.installEventFilter(self)
        controller.preferences_changed.connect(self.schedule)
        widget.composition_changed.connect(self.schedule)
        self.refresh()

    def schedule(self):
        self.timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.WindowStateChange}:
            self.schedule()
        return False

    def refresh(self):
        if self.controller._stopped:
            return
        dashboard_visible = self.dashboard.isVisible() and not self.dashboard.isMinimized()
        keys = set()
        if self.widget.isVisible() and not self.widget.isMinimized() and hasattr(self.widget, "composition"):
            keys.update(self.widget.composition.visible)
            if self.widget.details.isVisible():
                keys.update(self.widget.composition.details)
            if "battery_level" in keys:
                keys.add("charging_state")
        self.controller.set_sensor_demand("dashboard", DASHBOARD_SENSORS if dashboard_visible else ())
        self.controller.set_sensor_demand("widget", keys)


class LoadingIndicator(QWidget):
    def __init__(self, controller, parent, target=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.motion = motion_driver(controller)
        self.state = "ready"
        self.scale = 1
        self.corner = False
        self.target = target
        self.motion.add(self)
        parent.installEventFilter(self)
        if target is not None:
            target.installEventFilter(self)
        self.hide()

    def motion_active(self):
        return self.isVisible()

    def present(self, state: str, scale: float = 1):
        self.state, self.scale = state, scale
        active = state in {"loading", "refreshing"}
        tokens = TOKENS["loading"]
        size = max(8, round(tokens["fullSize" if state == "loading" else "compactSize"] * scale))
        parent = self.parentWidget()
        area = self.target.geometry() if self.target is not None else parent.rect()
        x = area.x() + (max(0, (area.width() - size) // 2) if state == "loading" else max(0, area.width() - size))
        y = round(tokens["inset"] * scale) if self.corner else area.y() + max(0, (area.height() - size) // 2)
        self.setGeometry(x, y, size, size)
        self.setAccessibleName(t("sensor_loading" if state == "loading" else "sensor_refreshing"))
        self.setVisible(active)
        if active:
            self.raise_()
            self.update()
        self.motion.schedule()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.Resize, QEvent.Type.Move}:
            self.present(self.state, self.scale)
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tokens = TOKENS["loading"]
        width = max(1, tokens["strokeWidth"] * self.scale)
        angle = -360 * self.motion.phase("connectingMark") if self.motion.enabled else 0
        inset = tokens["inset"] * self.scale
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        for role, stroke in (("canvas", width * 2), ("input", width)):
            painter.setPen(QPen(QColor(COLORS[role]), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(rect, round(angle * 16), 270 * 16)

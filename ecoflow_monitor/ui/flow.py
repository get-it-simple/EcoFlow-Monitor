import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import duration, number, update_status


class FlowWidget(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)
        self.setObjectName("flow")
        self.setMinimumSize(420, 407)
        self.status = QLabel(self)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def refresh(self):
        update_status(self.status, self.controller)
        self._position_status()
        active = self.isVisible() and self.controller.settings.animations and self.controller.fresh
        telemetry = self.controller.telemetry
        active = active and telemetry is not None and (telemetry.input_w > 0 or telemetry.output_w > 0)
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active:
            self._timer.stop()
        self.update()

    def _advance(self):
        self.phase = (self.phase + 0.017) % 1
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_status()

    def _position_status(self):
        scale = min(self.width() / 640, self.height() / 620)
        self.status.setGeometry(round(self.width() / 2 - 18 * scale), round((self.height() - 620 * scale) / 2 + 32 * scale), round(36 * scale), round(36 * scale))
        self.status.setStyleSheet(self.status.styleSheet() + f"font-size: {max(1, round(22 * scale))}px;")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = min(self.width() / 640, self.height() / 620)
        painter.translate((self.width() - 640 * scale) / 2, (self.height() - 620 * scale) / 2)
        painter.scale(scale, scale)
        telemetry = self.controller.telemetry
        ports = dict(telemetry.ports) if telemetry else {}
        fresh = self.controller.fresh
        foreground = "#f4f7f8" if fresh else "#96a3a9"
        green, orange = "#50e9a2", "#ffc16c"

        def text(rect, value, size=18, color=foreground, alignment=Qt.AlignmentFlag.AlignCenter):
            font = QFont("Segoe UI")
            font.setPixelSize(size)
            painter.setFont(font)
            painter.setPen(QColor(color))
            painter.drawText(QRectF(*rect), alignment, value)

        text((30, 2, 270, 32), t("input_power"), 21, green, Qt.AlignmentFlag.AlignLeft)
        text((350, 2, 260, 32), t("output_power"), 21, orange, Qt.AlignmentFlag.AlignRight)
        text((30, 36, 270, 60), number(telemetry.input_w if telemetry else None, "W"), 43, foreground, Qt.AlignmentFlag.AlignLeft)
        text((350, 36, 260, 60), number(telemetry.output_w if telemetry else None, "W"), 43, foreground, Qt.AlignmentFlag.AlignRight)

        def branch(x, incoming, value, label):
            y = 116 if incoming else 514
            path = QPainterPath(QPointF(x, y + (24 if incoming else -24)))
            path.cubicTo(x, 189 if incoming else 440, 320, 168 if incoming else 462, 320, 194 if incoming else 428)
            painter.setPen(QPen(QColor("#3b484d"), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            if fresh and value is not None and value > 0:
                progress = self.phase if incoming else 1 - self.phase
                if not self.controller.settings.animations:
                    progress = 0.5
                point = path.pointAtPercent(progress)
                painter.setBrush(QColor(green if incoming else orange))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(point, 4, 4)
            painter.setPen(QPen(QColor("#3c484d"), 1))
            painter.setBrush(QColor(0, 0, 0, 70))
            painter.drawEllipse(QPointF(x, y), 27, 27)
            text((x - 90, y - 14, 180, 29), label, 16, foreground)
            text((x - 95, y + 34, 190, 28), number(value, "W"), 21)

        branch(130, True, ports.get("ac_input"), "AC")
        branch(510, True, ports.get("pv_input"), "PV / DC")
        branch(130, False, ports.get("ac_output"), "AC")
        branch(510, False, ports.get("dc_output"), "DC")

        circle = QRectF(175, 195, 290, 290)
        gradient = QLinearGradient(0, 195, 0, 485)
        gradient.setColorAt(0, QColor(0, 0, 0, 70))
        gradient.setColorAt(1, QColor(0, 0, 0, 100))
        painter.setPen(QPen(QColor("#34464a"), 1.5))
        painter.setBrush(gradient)
        painter.drawEllipse(circle)
        if telemetry:
            painter.save()
            clipping = QPainterPath()
            clipping.addEllipse(circle)
            painter.setClipPath(clipping)
            water_y = 485 - 290 * telemetry.soc_pct / 100
            wave = QPainterPath(QPointF(170, 520))
            wave.lineTo(170, water_y)
            for x in range(170, 472, 5):
                amplitude = 4 if self._timer.isActive() else 0
                wave.lineTo(x, water_y + math.sin(x / 52 + self.phase * 2 * math.pi) * amplitude)
            wave.lineTo(472, 520)
            wave.closeSubpath()
            fill = QLinearGradient(0, 195, 0, 485)
            fill.setColorAt(0, QColor("#22e49a" if fresh else "#3b7460"))
            fill.setColorAt(1, QColor("#00b975" if fresh else "#244f43"))
            painter.fillPath(wave, fill)
            painter.restore()
        text((190, 270, 260, 80), number(telemetry.soc_pct if telemetry else None, "%"), 67)
        text((185, 364, 270, 28), t("estimated_runtime"), 16)
        text((185, 397, 270, 35), duration(self.controller.runtime), 23)

        for limit, color in ((self.controller.maximum, green), (self.controller.minimum, orange)):
            if limit.value is not None:
                y = 485 - 290 * limit.value / 100
                painter.setPen(QPen(QColor(color), 1, Qt.PenStyle.DotLine))
                painter.drawLine(QPointF(175, y), QPointF(465, y))
        usb = [(key, value) for key, value in ports.items() if key.startswith(("usb_", "anderson_"))]
        if usb:
            text((20, 570, 600, 26), "   ·   ".join((t(key) if not key.startswith("anderson_") else t("anderson")) + " " + number(value, "W") for key, value in usb[:4]), 14)
            if len(usb) > 4:
                text((20, 596, 600, 26), "   ·   ".join((t(key) if not key.startswith("anderson_") else t("anderson")) + " " + number(value, "W") for key, value in usb[4:]), 14)
        painter.end()

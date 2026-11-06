import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QHelpEvent, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QToolTip, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import duration, number, update_status
from ecoflow_monitor.ui.metrics import metric_label, metric_tooltip


class FlowWidget(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.phase = 0.0
        self._tooltip_position = None
        self._tooltip_text = ""
        self._hovered_metric = ""
        self._hover_position = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)
        self.setObjectName("flow")
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 407)
        self.status = QLabel(self)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def refresh(self):
        update_status(self.status, self.controller)
        self._position_status()
        if self._hover_position is not None:
            self._hovered_metric = self.metric_at(self._hover_position)
        if self._tooltip_position is not None and QToolTip.isVisible() and QToolTip.text() == self._tooltip_text:
            self._tooltip_text = self.tooltip_at(self._tooltip_position)
            if self._tooltip_text:
                QToolTip.showText(self.mapToGlobal(self._tooltip_position.toPoint()), self._tooltip_text, self)
            else:
                QToolTip.hideText()
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
        if self._hover_position is not None:
            self._hovered_metric = self.metric_at(self._hover_position)

    def _position_status(self):
        scale = min(self.width() / 640, self.height() / 620)
        self.status.setGeometry(round(self.width() / 2 - 18 * scale), round((self.height() - 620 * scale) / 2 + 32 * scale), round(36 * scale), round(36 * scale))
        self.status.setStyleSheet(self.status.styleSheet() + f"font-size: {max(1, round(22 * scale))}px;")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        self._timer.stop()
        self._hovered_metric = ""
        self._hover_position = None
        super().hideEvent(event)

    def _drawing_transform(self):
        scale = min(self.width() / 640, self.height() / 620)
        return scale, QPointF((self.width() - 640 * scale) / 2, (self.height() - 620 * scale) / 2)

    def _usb_cells(self):
        telemetry = self.controller.telemetry
        ports = [(key, value) for key, value in telemetry.ports if key.startswith(("usb_", "anderson_"))] if telemetry else []
        cells = []
        for start in range(0, len(ports), 4):
            row = ports[start:start + 4]
            width = 600 / len(row)
            for column, (key, value) in enumerate(row):
                cells.append((QRectF(20 + column * width, 570 + start // 4 * 26, width, 26), key, value))
        return cells

    def _metric_regions(self):
        regions = [
            (QRectF(30, 2, 270, 94), "input_power"),
            (QRectF(350, 2, 260, 94), "output_power"),
            (QRectF(35, 89, 190, 89), "ac_input"),
            (QRectF(415, 89, 190, 89), "pv_input"),
            (QRectF(35, 487, 190, 75), "ac_output"),
            (QRectF(415, 487, 190, 75), "dc_output"),
            (QRectF(185, 364, 270, 68), "estimated_runtime"),
            (QRectF(190, 238, 260, 112), "battery_level"),
        ]
        regions.extend((rect, key) for rect, key, _ in self._usb_cells())
        return regions

    def metric_at(self, position: QPointF) -> str:
        scale, offset = self._drawing_transform()
        if scale <= 0:
            return ""
        point = (position - offset) / scale
        return next((key for rect, key in self._metric_regions() if rect.contains(point)), "")

    def value_visible(self, key: str) -> bool:
        return not self.controller.settings.monitor_values_on_hover or self._hovered_metric == key

    def mouseMoveEvent(self, event):
        self._hover_position = event.position()
        key = self.metric_at(self._hover_position)
        if key != self._hovered_metric:
            self._hovered_metric = key
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_position = None
        self._hovered_metric = ""
        self.update()
        super().leaveEvent(event)

    def tooltip_at(self, position: QPointF) -> str:
        scale, offset = self._drawing_transform()
        if scale <= 0:
            return ""
        point = (position - offset) / scale
        limits = []
        for key, limit in (("minimum_discharge_level", self.controller.minimum), ("maximum_charge_level", self.controller.maximum)):
            if limit.value is not None and QRectF(175, 485 - 290 * limit.value / 100 - 5, 290, 10).contains(point):
                limits.append(metric_tooltip(key, self.controller))
        if limits:
            return "\n\n".join(limits)
        key = self.metric_at(position)
        return metric_tooltip(key, self.controller) if key else ""

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            self._tooltip_position = QPointF(event.pos())
            tooltip = self.tooltip_at(self._tooltip_position)
            self._tooltip_text = tooltip
            if tooltip:
                QToolTip.showText(event.globalPos(), tooltip, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale, offset = self._drawing_transform()
        painter.translate(offset)
        painter.scale(scale, scale)
        telemetry = self.controller.telemetry
        ports = dict(telemetry.ports) if telemetry else {}
        fresh = self.controller.fresh
        foreground = "#f4f7f8" if fresh else "#96a3a9"
        green, orange = "#50e9a2", "#ffc16c"

        def text(rect, value, size=18, color=foreground, alignment=Qt.AlignmentFlag.AlignCenter, metric="", label=False):
            if label and not self.controller.settings.monitor_labels_visible:
                return
            if metric and not self.value_visible(metric):
                return
            font = QFont("Segoe UI")
            font.setPixelSize(size)
            painter.setFont(font)
            painter.setPen(QColor(color))
            painter.drawText(QRectF(*rect), alignment, value)

        text((30, 2, 270, 32), t("input_power"), 21, green, Qt.AlignmentFlag.AlignLeft, label=True)
        text((350, 2, 260, 32), t("output_power"), 21, orange, Qt.AlignmentFlag.AlignRight, label=True)
        text((30, 36, 270, 60), number(telemetry.input_w if telemetry else None, t("unit_watt")), 43, foreground, Qt.AlignmentFlag.AlignLeft, metric="input_power")
        text((350, 36, 260, 60), number(telemetry.output_w if telemetry else None, t("unit_watt")), 43, foreground, Qt.AlignmentFlag.AlignRight, metric="output_power")

        def branch(x, incoming, value, key):
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
            text((x - 95, y - 24, 190, 48), t(key), 16, foreground, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, label=True)
            text((x - 95, y + 34, 190, 28), number(value, t("unit_watt")), 21, metric=key)

        branch(130, True, ports.get("ac_input"), "ac_input")
        branch(510, True, ports.get("pv_input"), "pv_input")
        branch(130, False, ports.get("ac_output"), "ac_output")
        branch(510, False, ports.get("dc_output"), "dc_output")

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
        text((190, 238, 260, 28), t("battery_level"), 18, label=True)
        text((190, 270, 260, 80), number(telemetry.soc_pct if telemetry else None, "%"), 67, metric="battery_level")
        text((185, 364, 270, 28), t("estimated_runtime"), 16, label=True)
        text((185, 397, 270, 35), duration(self.controller.runtime), 23, metric="estimated_runtime")

        for limit, color in ((self.controller.maximum, green), (self.controller.minimum, orange)):
            if limit.value is not None:
                y = 485 - 290 * limit.value / 100
                painter.setPen(QPen(QColor(color), 1, Qt.PenStyle.DotLine))
                painter.drawLine(QPointF(175, y), QPointF(465, y))
        for cell_rect, key, port_power in self._usb_cells():
            parts = [metric_label(key)] if self.controller.settings.monitor_labels_visible else []
            if self.value_visible(key):
                parts.append(number(port_power, t("unit_watt")))
            text((cell_rect.x(), cell_rect.y(), cell_rect.width(), cell_rect.height()), " ".join(parts), 14)
        painter.end()

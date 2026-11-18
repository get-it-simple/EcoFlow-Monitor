import math
from bisect import bisect_left, bisect_right

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ecoflow_monitor.ui.motion import motion_driver
from ecoflow_monitor.ui.tokens import COLORS


class InfinityIndicator(QWidget):
    def __init__(self, controller, parent):
        super().__init__(parent)
        self.controller = controller
        self.motion = motion_driver(controller)
        self.motion.add(self)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.points = []
        self.lengths = []
        self.outline = QPainterPath()
        self.charge_path = QPainterPath()
        self._fraction = None
        self.hide()

    def motion_active(self):
        presentation = self.controller.remaining_presentation
        return self.isVisible() and self.motion.state.status == "online" and presentation.mode == "infinity" and bool(presentation.charge_fraction)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.build_path()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "points"):
            self.build_path()

    def build_path(self):
        width = max(0, min(self.width() - 8, (self.height() - 8) * 2.4, self.fontMetrics().height() * 2.4))
        height = width / 2.4
        self.path_height = height
        self.points = []
        self.lengths = [0.0]
        self.outline = QPainterPath()
        self._fraction = None
        for index in range(241):
            angle = index * math.tau / 240
            point = QPointF(self.width() / 2 + width / 2 * math.sin(angle), self.height() / 2 + height / 2 * math.sin(2 * angle))
            if self.points:
                distance = point - self.points[-1]
                self.lengths.append(self.lengths[-1] + math.hypot(distance.x(), distance.y()))
            self.points.append(point)
            if index == 0:
                self.outline.moveTo(point)
            else:
                self.outline.lineTo(point)

    def point_at(self, distance):
        index = min(len(self.points) - 1, max(1, bisect_left(self.lengths, distance)))
        start, end = self.lengths[index - 1], self.lengths[index]
        return self.points[index - 1] + (self.points[index] - self.points[index - 1]) * ((distance - start) / (end - start) if end > start else 0)

    def segment(self, start, end):
        path = QPainterPath(self.point_at(start))
        for index in range(bisect_right(self.lengths, start), bisect_left(self.lengths, end)):
            path.lineTo(self.points[index])
        path.lineTo(self.point_at(end))
        return path

    def paintEvent(self, event):
        presentation = self.controller.remaining_presentation
        if not self.points or presentation.charge_fraction is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        stroke = max(2, self.path_height / 8)
        total = self.lengths[-1]
        if total <= 0:
            return
        limit = total * presentation.charge_fraction
        for role, width in (("canvas", stroke + 4), ("border", stroke)):
            painter.setPen(QPen(QColor(COLORS[role]), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(self.outline)
        if limit <= 0:
            return
        if presentation.charge_fraction != self._fraction:
            self._fraction = presentation.charge_fraction
            self.charge_path = self.segment(0, limit)
        painter.setPen(QPen(QColor(COLORS["accent"]), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(self.charge_path)
        if self.motion.enabled and self.motion.state.status == "online":
            start = limit * self.motion.phase("connectingMark")
            end = start + min(total * 0.08, limit * 0.25)
            painter.setPen(QPen(QColor(COLORS["accentHover"]), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(self.segment(start, min(limit, end)))
            if end > limit:
                painter.drawPath(self.segment(0, end - limit))

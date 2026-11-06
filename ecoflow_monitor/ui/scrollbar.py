from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractScrollArea, QScrollBar, QStyle, QStyleOptionSlider, QWidget

from ecoflow_monitor.ui.theme import bind_style, current_accent


class QuietScrollBar(QScrollBar):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setMouseTracking(True)
        self._style_binding = bind_style(self, self._style)

    def _scale(self):
        return max(0.5, self.fontMetrics().height() / 16)

    def _style(self, _accent):
        scale = self._scale()
        self.update()
        return f"""
QScrollBar {{ background: transparent; border: none; margin: 0; }}
QScrollBar:vertical {{ width: {max(6, round(12 * scale))}px; }}
QScrollBar:horizontal {{ height: {max(6, round(12 * scale))}px; }}
QScrollBar::handle {{ background: transparent; min-width: {round(24 * scale)}px; min-height: {round(24 * scale)}px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: transparent; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""

    def thumb_rect(self):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        rect = QRectF(self.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option, QStyle.SubControl.SC_ScrollBarSlider, self))
        thickness = max(2, round((7 if self.underMouse() or self.isSliderDown() else 3) * self._scale()))
        if self.orientation() == Qt.Orientation.Vertical:
            rect.setLeft((self.width() - thickness) / 2)
            rect.setWidth(thickness)
        else:
            rect.setTop((self.height() - thickness) / 2)
            rect.setHeight(thickness)
        return rect

    def paintEvent(self, event):
        if self.maximum() <= self.minimum():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(current_accent())
        color.setAlpha(190 if self.underMouse() or self.isSliderDown() else 85)
        if not self.isEnabled():
            color.setAlpha(45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        rect = self.thumb_rect()
        radius = min(rect.width(), rect.height()) / 2
        painter.drawRoundedRect(rect, radius, radius)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


def install_scrollbars(root: QWidget):
    for area in root.findChildren(QAbstractScrollArea):
        for orientation in (Qt.Orientation.Vertical, Qt.Orientation.Horizontal):
            current = area.verticalScrollBar() if orientation == Qt.Orientation.Vertical else area.horizontalScrollBar()
            if isinstance(current, QuietScrollBar):
                continue
            bar = QuietScrollBar(orientation, area)
            if orientation == Qt.Orientation.Vertical:
                area.setVerticalScrollBar(bar)
            else:
                area.setHorizontalScrollBar(bar)

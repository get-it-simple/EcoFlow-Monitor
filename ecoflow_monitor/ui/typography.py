from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QLabel

from ecoflow_monitor.ui.tokens import COLORS


def draw_outlined(painter, text, font, rect, brush, wrap):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere if wrap else QTextOption.WrapMode.NoWrap)
    layout.setTextOption(option)
    layout.beginLayout()
    lines = []
    height = 0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1, rect.width() - 4))
        lines.append((line, height))
        height += line.height()
    layout.endLayout()
    offset = max(0, (rect.height() - height) / 2)
    for line, position in lines:
        content = text[line.textStart():line.textStart() + line.textLength()]
        path.addText(QPointF(rect.x() + 2, rect.y() + offset + position + line.ascent()), font, content)
    pen = QPen(QColor(COLORS["canvas"]), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.strokePath(path, pen)
    painter.fillPath(path, brush)


class ValueLabel(QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outlined = False

    def paintEvent(self, event):
        if not self.outlined:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        draw_outlined(painter, self.text(), self.font(), self.contentsRect(), self.palette().windowText(), self.wordWrap())

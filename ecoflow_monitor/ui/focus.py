from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ecoflow_monitor.ui.tokens import COLORS, TOKENS


class FocusOutline(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(COLORS["focus"]), TOKENS["focus"]["width"]))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)


class FocusBinding(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.outline = FocusOutline(window)
        QApplication.instance().focusChanged.connect(self.refresh)
        window.installEventFilter(self)

    def refresh(self, *args):
        widget = QApplication.focusWidget()
        if widget is None or widget.window() is not self.window or not widget.isVisible():
            self.outline.hide()
            return
        gap = TOKENS["focus"]["offset"] + TOKENS["focus"]["width"]
        rect = widget.rect().translated(widget.mapTo(self.window, widget.rect().topLeft())).adjusted(-gap, -gap, gap, gap)
        self.outline.setGeometry(rect)
        self.outline.show()
        self.outline.raise_()
        self.outline.update()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide}:
            self.refresh()
        return False

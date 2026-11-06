from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ecoflow_monitor.i18n import t


class ResizeEdge(QWidget):
    def __init__(self, edges, parent):
        super().__init__(parent)
        self.edges = edges
        self.setObjectName("resize_edge")
        cursors = {
            Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.TopEdge | Qt.Edge.LeftEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.BottomEdge | Qt.Edge.RightEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.TopEdge | Qt.Edge.RightEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.BottomEdge | Qt.Edge.LeftEdge: Qt.CursorShape.SizeBDiagCursor,
        }
        self.setCursor(cursors[edges])

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemResize(self.edges):
                event.accept()
                return
        super().mousePressEvent(event)


class ResizeGrip(ResizeEdge):
    def __init__(self, parent):
        super().__init__(Qt.Edge.BottomEdge | Qt.Edge.RightEdge, parent)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#d0d0d0" if self.underMouse() else "#808080"), max(1.0, self.width() / 18)))
        step = self.width() / 4
        for index in (1, 2, 3):
            painter.drawLine(round(self.width() - index * step), self.height() - 3, self.width() - 3, round(self.height() - index * step))


class WindowResize(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.scale = 1.0
        self.grip = ResizeGrip(window)
        self.grip.setObjectName("resize_grip")
        self.grip.hide()
        left, top, right, bottom = Qt.Edge.LeftEdge, Qt.Edge.TopEdge, Qt.Edge.RightEdge, Qt.Edge.BottomEdge
        self.edges = [ResizeEdge(edge, window) for edge in (left, top, right, bottom, left | top, right | top, left | bottom, right | bottom)]
        window.installEventFilter(self)
        self.layout()

    def layout(self):
        scale = self.scale
        border = max(3, round(6 * scale))
        width, height = self.window.width(), self.window.height()
        rects = (
            QRect(0, border, border, max(0, height - 2 * border)),
            QRect(border, 0, max(0, width - 2 * border), border),
            QRect(width - border, border, border, max(0, height - 2 * border)),
            QRect(border, height - border, max(0, width - 2 * border), border),
            QRect(0, 0, border, border),
            QRect(width - border, 0, border, border),
            QRect(0, height - border, border, border),
            QRect(width - border, height - border, border, border),
        )
        active = not self.window.isMaximized() and not self.window.isFullScreen()
        for edge, rect in zip(self.edges, rects):
            edge.setGeometry(rect)
            edge.setVisible(active)
            edge.raise_()
        size = max(12, round(18 * scale))
        self.grip.setGeometry(width - size - border, height - size - border, size, size)
        self.grip.setToolTip(t("resize_window"))
        self.grip.setAccessibleName(t("resize_window"))
        self.grip.setVisible(active and self.window.underMouse())
        self.grip.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.WindowStateChange, QEvent.Type.Enter):
            self.layout()
        elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide):
            self.grip.hide()
        return False

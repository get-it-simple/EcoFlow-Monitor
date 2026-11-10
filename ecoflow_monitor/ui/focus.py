from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit, QWidget

from ecoflow_monitor.ui.tokens import COLORS, TOKENS


class KeyboardFocus(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            QApplication.instance().setProperty("keyboard_focus", True)
        elif event.type() == QEvent.Type.MouseButtonPress:
            QApplication.instance().setProperty("keyboard_focus", False)
            focus = QApplication.focusWidget()
            binding = getattr(focus.window(), "_focus_binding", None) if focus is not None else None
            if binding is not None:
                binding.keyboard = False
                binding.outline.hide()
        return False


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
        self.keyboard = False
        self.outline = FocusOutline(window)
        app = QApplication.instance()
        app.focusChanged.connect(self.refresh)
        window.installEventFilter(self)
        if not hasattr(app, "ecoflow_keyboard_focus"):
            app.ecoflow_keyboard_focus = KeyboardFocus(app)
            app.installEventFilter(app.ecoflow_keyboard_focus)

    def refresh(self, *args):
        widget = QApplication.focusWidget()
        if widget is None or widget.window() is not self.window or not widget.isVisible() or not (self.keyboard or QApplication.instance().property("keyboard_focus")):
            self.outline.hide()
            return
        while widget.parentWidget() is not None and isinstance(widget.parentWidget(), (QAbstractSpinBox, QComboBox)):
            widget = widget.parentWidget()
        if not isinstance(widget, (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit)) and not widget.property("focus_target"):
            self.outline.hide()
            return
        gap = TOKENS["focus"]["offset"] + TOKENS["focus"]["width"]
        rect = widget.rect().translated(widget.mapTo(self.window, widget.rect().topLeft())).adjusted(-gap, -gap, gap, gap)
        self.outline.setGeometry(rect)
        self.outline.show()
        self.outline.raise_()
        self.outline.update()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() in {QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide}:
            self.refresh()
        return False

    def stop(self):
        QApplication.instance().focusChanged.disconnect(self.refresh)
        self.outline.hide()

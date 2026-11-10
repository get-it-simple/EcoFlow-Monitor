from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QApplication, QFrame, QLayout, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import metric_text
from ecoflow_monitor.ui.metrics import metric_label, metric_tooltip
from ecoflow_monitor.ui.tokens import COLORS
from ecoflow_monitor.ui.typography import ValueLabel


TONES = {"battery_level": "accent", "input_power": "input", "output_power": "output"}


class MetricReading(QFrame):
    def __init__(self, controller, key, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.key = key
        self.hover_only = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.box = QVBoxLayout(self)
        self.box.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)
        self.label = ValueLabel(self)
        self.label.setWordWrap(True)
        self.value = ValueLabel(self)
        policy = self.value.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.value.setSizePolicy(policy)
        self.box.addWidget(self.label)
        self.box.addWidget(self.value)
        self.configure(1)
        self.refresh()

    def configure(self, scale, size=24, clear=False, labels=True, hover=False):
        signature = (scale, size, clear, labels, hover)
        if getattr(self, "_configuration", None) == signature:
            self.refresh()
            return
        self._configuration = signature
        self.hover_only = hover
        self.label.setVisible(labels)
        for label, pixels, weight, role in ((self.label, 12, 400, "muted"), (self.value, size, 600, TONES.get(self.key, "text"))):
            font = QFont(self.font())
            font.setPixelSize(max(1, round(pixels * scale)))
            font.setWeight(QFont.Weight(weight))
            label.setFont(font)
            label.setStyleSheet(f"color: {COLORS[role]}; background: transparent; font-size: {max(1, round(pixels * scale))}px; font-weight: {weight};")
            label.outlined = clear
            label.ensurePolished()
            label.setMinimumHeight(max(round(pixels * 1.25 * scale), label.fontMetrics().height()))
        self.setMinimumWidth(round(100 * scale))
        self.refresh()

    def refresh(self):
        text = metric_text(self.controller, self.key)
        self.label.setText(metric_label(self.key))
        self.value.setText(text)
        self.value.setVisible(not self.hover_only or self.underMouse() or self.hasFocus())
        tooltip = metric_tooltip(self.key, self.controller)
        if text == "—":
            tooltip += "\n" + t("missing_data")
        self.setToolTip(tooltip)
        self.setAccessibleName(metric_label(self.key) + ": " + text)
        self.value.setAccessibleName(self.accessibleName())

    def event(self, event):
        result = super().event(event)
        if event.type() in {QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut} and hasattr(self, "value"):
            self.refresh()
        return result


class EnergyBalance(QWidget):
    def __init__(self, controller, parent=None, battery=False):
        super().__init__(parent)
        self.controller = controller
        self.battery = battery
        self.phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)
        self.setMinimumHeight(12)

    def _advance(self):
        self.phase = (self.phase + 0.02) % 1
        self.update()

    def refresh(self):
        telemetry = self.controller.telemetry
        active = self.isVisible() and self.controller.fresh and self.controller.ui_settings.animations and QApplication.instance().property("system_animations") is not False and telemetry is not None and telemetry.input_w + telemetry.output_w > 0
        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        telemetry = self.controller.telemetry
        total = telemetry.input_w + telemetry.output_w if telemetry else 0
        incoming = telemetry.input_w / total if total > 0 else 0
        if self.battery:
            incoming = max(0, min(1, telemetry.soc_pct / 100)) if telemetry else 0
        rect = QRectF(0, self.height() / 2 - 4, self.width(), 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["border"]))
        painter.drawRoundedRect(rect, 4, 4)
        if total > 0 or self.battery:
            painter.setBrush(QColor(COLORS["accent"] if self.battery else COLORS["input"]))
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width() * incoming, rect.height()), 4, 4)
            if not self.battery:
                painter.setBrush(QColor(COLORS["output"]))
                painter.drawRoundedRect(QRectF(rect.width() * incoming, rect.y(), rect.width() * (1 - incoming), rect.height()), 4, 4)
        if self._timer.isActive():
            painter.setBrush(QColor(COLORS["text"]))
            painter.drawEllipse(QPointF(self.width() * self.phase, self.height() / 2), 3, 3)

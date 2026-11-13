from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import number
from ecoflow_monitor.ui.metrics import metric_tooltip
from ecoflow_monitor.ui.tokens import COLORS
from ecoflow_monitor.ui.polling import LoadingIndicator


class LimitReadout(QLabel):
    def __init__(self, controller, key, parent):
        super().__init__(parent)
        self.controller = controller
        self.key = key
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.label = ""
        self.value = ""
        self.loader = LoadingIndicator(controller, self)

    def refresh(self):
        limit = self.controller.minimum if self.key == "minimum_discharge_level" else self.controller.maximum
        self.label = t("minimum_short" if self.key == "minimum_discharge_level" else "maximum_short")
        self.value = number(limit.value, "%")
        state = self.controller.reading_state(self.key)
        if state == "loading":
            self.value = "     "
        self.loader.present(state, self.font().pixelSize() / 14 if self.font().pixelSize() > 0 else 1)
        self.setText(self.label + ": " + self.value)
        self.setToolTip(metric_tooltip(self.key, self.controller))
        self.setAccessibleName(self.text())
        self.update()

    def display_text(self):
        settings = self.controller.ui_settings
        label = self.label if settings.monitor_labels_visible else ""
        value = self.value if not settings.monitor_values_on_hover or self.underMouse() or self.hasFocus() else ""
        return ": ".join(part for part in (label, value) if part)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, self.display_text())

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


class ChargeLimits(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.readouts = [LimitReadout(controller, key, self) for key in ("minimum_discharge_level", "maximum_charge_level")]
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for readout in self.readouts:
            layout.addWidget(readout)
        self.setStyleSheet(f"color: {COLORS['muted']};")

    def refresh(self):
        for readout in self.readouts:
            readout.refresh()
        self.setToolTip("\n\n".join(readout.toolTip() for readout in self.readouts))

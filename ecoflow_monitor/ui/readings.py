from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QLayout, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import metric_text
from ecoflow_monitor.ui.metrics import metric_label, metric_tooltip
from ecoflow_monitor.ui.tokens import COLORS, RADIUS
from ecoflow_monitor.ui.typography import ValueLabel
from ecoflow_monitor.ui.motion import MotionSurface, motion_driver, wave


TONES = {"battery_level": "accent", "input_power": "input", "output_power": "output"}


class MetricReading(MotionSurface):
    def __init__(self, controller, key, parent=None):
        super().__init__(controller, {"battery_level": "battery", "input_power": "input", "output_power": "output"}.get(key), parent)
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
        self.motion_radius = RADIUS["card"] * scale
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
        self.motion.refresh()
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
        if self.key == "battery_level":
            color = COLORS["output" if self.motion.state.mode == "discharging" else "accent"]
            self.value.setStyleSheet(f"color: {color}; background: transparent; font-size: {self.value.font().pixelSize()}px; font-weight: 600;")

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
        self._scale = 1
        self.motion = motion_driver(controller)
        self.motion.add(self)
        self.setMinimumHeight(12)

    def configure(self, scale, material="Panel"):
        self._scale = scale
        self.material = material
        self.setMinimumHeight(round(12 * scale))

    def motion_active(self):
        role, gain = self.motion.roles["battery" if self.battery else "balance"].sample(self.motion.now())
        return role is not None or self.motion.values["soc" if self.battery else "input_share"].active(self.motion.now())

    def refresh(self):
        self.motion.refresh()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.motion.schedule()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        state = self.motion.state
        incoming = self.motion.values["soc" if self.battery else "input_share"].value(self.motion.now())
        known = incoming is not None
        incoming = (incoming / 100 if self.battery else incoming) if known else 0
        self.material = getattr(self, "material", "Panel")
        radius = 4 * self._scale
        rect = QRectF(0, self.height() / 2 - radius, self.width(), 2 * radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["border"]))
        painter.drawRoundedRect(rect, radius, radius)
        if known:
            painter.setBrush(QColor(COLORS["output" if state.mode == "discharging" else "accent"] if self.battery else COLORS["input"]))
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width() * incoming, rect.height()), radius, radius)
            if not self.battery:
                painter.setBrush(QColor(COLORS["output"]))
                painter.drawRoundedRect(QRectF(rect.width() * incoming, rect.y(), rect.width() * (1 - incoming), rect.height()), radius, radius)
        if known and self.motion.enabled:
            role, gain = self.motion.roles["battery" if self.battery else "balance"].sample(self.motion.now())
            segment = QRectF(rect.x(), rect.y(), rect.width() * incoming, rect.height()) if self.battery or role == "input" else QRectF(rect.width() * incoming, rect.y(), rect.width() * (1 - incoming), rect.height())
            wave(painter, segment, role, self.motion.phase("fieldWave" if self.battery else "balanceWave"), self.material, gain, radius)

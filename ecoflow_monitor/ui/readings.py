from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QLayout, QSizePolicy, QVBoxLayout, QWidget

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
        self.fit_values = False
        self._value_bottom = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.box = QVBoxLayout(self)
        self.box.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)
        self.label = ValueLabel(self)
        self.label.setWordWrap(True)
        self.value = ValueLabel(self)
        self.value.setWordWrap(key == "estimated_runtime")
        from ecoflow_monitor.ui.polling import LoadingIndicator
        self.loader = LoadingIndicator(controller, self.value)
        self.infinity = None
        if key == "estimated_runtime":
            from ecoflow_monitor.ui.infinity import InfinityIndicator
            self.infinity = InfinityIndicator(controller, self.value)
        policy = self.value.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.value.setSizePolicy(policy)
        self.box.addWidget(self.label)
        self.box.addWidget(self.value, 1)
        self.configure(1)
        self.refresh()

    def configure(self, scale, size=24, clear=False, labels=True, hover=False, padding=0, minimum_width=100, label_width=None, *, fit=False, reserved_bottom=0):
        self.motion_radius = RADIUS["card"] * scale
        signature = (scale, size, clear, labels, hover, padding, minimum_width, label_width, fit, reserved_bottom)
        if getattr(self, "_configuration", None) == signature:
            self.refresh()
            return
        self._configuration = signature
        self.fit_values = fit
        self._value_bottom = round(reserved_bottom * scale)
        self._fit_signature = None
        self.value.setSizePolicy(QSizePolicy.Policy.Ignored if fit else QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Ignored if fit else QSizePolicy.Policy.Preferred)
        self.label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.label_width = label_width
        self.label.setWordWrap(label_width is None)
        inset = round(padding * scale)
        self.box.setContentsMargins(inset, inset, inset, inset + self._value_bottom)
        self.box.setSpacing(round(4 * scale))
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
        self.setMinimumWidth(round(minimum_width * scale))
        self.refresh()

    def baseline_size(self, width=None):
        scale, pixels, clear, labels, hover, padding = self._configuration[:6]
        font = QFont(self.value.font())
        font.setPixelSize(max(1, round(pixels * scale)))
        metrics = QFontMetricsF(font)
        text = metric_text(self.controller, self.key)
        inset = round(padding * scale)
        inner = max(1, width - 2 * inset) if width is not None else metrics.horizontalAdvance(text) + 4
        flags = Qt.TextFlag.TextWordWrap if self.value.wordWrap() else Qt.TextFlag.TextSingleLine
        bounds = metrics.boundingRect(QRectF(0, 0, inner, 1000000), flags, text)
        height = max(round(pixels * 1.25 * scale), metrics.height(), bounds.height())
        if labels:
            height += max(self.label.minimumHeight(), self.label.fontMetrics().height(), self.label.heightForWidth(round(inner))) + self.box.spacing()
        return QSize(round(max(bounds.width() + 4, self.label.minimumSizeHint().width() if labels else 0) + 2 * inset),
                     round(height + 2 * inset + self._value_bottom))

    def sizeHint(self):
        return self.baseline_size() if self.fit_values else super().sizeHint()

    def minimumSizeHint(self):
        return self.baseline_size(self.width()) if self.fit_values else super().minimumSizeHint()

    def _value_font(self, pixels):
        color = COLORS["output" if self.key == "battery_level" and self.motion.state.mode == "discharging" else TONES.get(self.key, "text")]
        style = f"color: {color}; background: transparent; font-size: {pixels}px; font-weight: 600;"
        if self.value.font().pixelSize() != pixels:
            font = QFont(self.value.font())
            font.setPixelSize(pixels)
            self.value.setFont(font)
        if self.value.styleSheet() != style:
            self.value.setStyleSheet(style)

    def fit_value(self):
        if not self.fit_values:
            return
        self.box.activate()
        rect = self.value.contentsRect().adjusted(2, 2, -2, -2)
        text = metric_text(self.controller, self.key)
        signature = (rect.width(), rect.height(), text, self.value.font().family(), self.value.wordWrap())
        if getattr(self, "_fit_signature", None) != signature:
            self._fit_signature = signature
            scale, size = self._configuration[:2]
            baseline = max(1, round(size * scale))
            lower, upper = baseline, max(baseline, rect.height())
            font = QFont(self.value.font())
            flags = Qt.TextFlag.TextWordWrap if self.value.wordWrap() else Qt.TextFlag.TextSingleLine
            while lower < upper:
                pixels = (lower + upper + 1) // 2
                font.setPixelSize(pixels)
                metrics = QFontMetricsF(font)
                bounds = metrics.boundingRect(QRectF(0, 0, max(1, rect.width()), 1000000), flags, text)
                if bounds.width() <= rect.width() and max(bounds.height(), pixels * 1.25) <= rect.height():
                    lower = pixels
                else:
                    upper = pixels - 1
            self._fitted_pixels = lower
        self._value_font(self._fitted_pixels)

    def refresh(self):
        self.motion.refresh()
        text = metric_text(self.controller, self.key)
        state = self.controller.reading_state(self.key)
        infinite = False
        if self.infinity is not None:
            mode = self.controller.remaining_presentation.mode
            infinite = mode == "infinity"
            if mode == "waiting":
                state = "loading"
            self.infinity.setGeometry(self.value.rect())
            self.infinity.setVisible(infinite)
            self.infinity.update()
            self.motion.schedule()
        display = " " if state == "loading" else text
        if infinite:
            display = " "
            state = "ready"
        label = metric_label(self.key, self.controller)
        width = getattr(self, "label_width", None)
        self.label.setText(self.label.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, width) if width is not None else label)
        self.label.setToolTip(label)
        self.value.setText(display)
        self._layout_loader(state)
        self.value.setVisible(not self.hover_only or self.underMouse() or self.hasFocus())
        tooltip = metric_tooltip(self.key, self.controller)
        if state in {"loading", "refreshing", "stale"} or state == "missing" and self.controller.polling.samples:
            tooltip += "\n" + t("sensor_" + state)
        if text == "—":
            tooltip += "\n" + t("missing_data")
        self.setToolTip(tooltip)
        self.setAccessibleName(label + ": " + (t("sensor_" + state) if state in {"loading", "refreshing"} else text))
        self.value.setAccessibleName(self.accessibleName())
        if self.fit_values:
            self.fit_value()
        elif self.key == "battery_level":
            self._value_font(self.value.font().pixelSize())

    def event(self, event):
        result = super().event(event)
        if event.type() in {QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut} and hasattr(self, "value"):
            self.refresh()
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "loader"):
            self.fit_value()
            self._layout_loader(self.controller.reading_state(self.key))

    def _layout_loader(self, state):
        if self.infinity is not None:
            mode = self.controller.remaining_presentation.mode
            state = "loading" if mode == "waiting" else "ready" if mode == "infinity" else state
            self.infinity.setGeometry(self.value.rect())
        anchor = self if state == "refreshing" else self.value
        if self.loader.parentWidget() is not anchor:
            self.loader.setParent(anchor)
        self.loader.corner = state == "refreshing"
        self.loader.present(state, self._configuration[0])


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

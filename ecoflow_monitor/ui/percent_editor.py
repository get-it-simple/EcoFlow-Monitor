from PySide6.QtCore import QEvent, QRectF, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QGridLayout, QSlider, QStyle, QStyleOptionSlider, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.components import NumberField
from ecoflow_monitor.ui.tokens import COLORS


class _PercentSlider(QSlider):
    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = QRectF(self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                                     QStyle.SubControl.SC_SliderHandle, self))
        center = handle.center()
        radius = min(handle.width(), handle.height()) / 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["accent" if self.isEnabled() else "subtle"]))
        painter.drawEllipse(center, radius, radius)
        if self.isEnabled() and self.hasFocus():
            width = max(1, round(2 * self.font().pixelSize() / 14))
            painter.setPen(QPen(QColor(COLORS["focus"]), width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius - width / 2, radius - width / 2)


class PercentEditor(QWidget):
    valueChanged = Signal(float)

    def __init__(self, minimum=0, maximum=100, decimals=0, optional=False, default=0, compact=False, parent=None):
        super().__init__(parent)
        self.setProperty("wide_field", not compact)
        self.spin = NumberField()
        self.spin.setRange(minimum, maximum)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(" %")
        self.spin.setValue(default)
        self.slider = _PercentSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(round(self.spin.value()))
        self.slider.setMinimumWidth(80)
        self.slider.setAccessibleName(t("percent_quick_select"))
        self.toggle = QCheckBox() if optional else None
        if self.toggle is not None:
            self.toggle.setToolTip(t("percent_enabled"))
            self.toggle.setAccessibleName(t("percent_enabled"))
            if compact:
                self.toggle.setText("")
        self._compact = compact
        self._stacked = None
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        if self.toggle is not None:
            self.toggle.toggled.connect(self._enabled_changed)
        self._reflow()
        self.setFocusProxy(self.spin)
        self.spin.valueChanged.connect(self._spin_changed)
        self.slider.valueChanged.connect(self.spin.setValue)
        self._sync_enabled()

    def _reflow(self):
        scale = max(0.5, self.font().pixelSize() / 14)
        self.slider.setMinimumHeight(round(24 * scale))
        stacked = self._compact or self.width() < round(280 * scale)
        if stacked == self._stacked:
            return
        self._stacked = stacked
        layout = self.layout()
        for widget in (self.toggle, self.spin, self.slider):
            if widget is not None:
                layout.removeWidget(widget)
        column = int(self.toggle is not None)
        if self.toggle is not None:
            layout.addWidget(self.toggle, 0, 0)
        layout.addWidget(self.spin, 0, column)
        if stacked:
            layout.addWidget(self.slider, 1, 0, 1, column + 1)
        else:
            layout.addWidget(self.slider, 0, column + 1)
        layout.setColumnStretch(column, 1)
        layout.setColumnStretch(column + 1, 0 if stacked else 2)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_compact"):
            self._reflow()

    def value(self) -> float:
        return self.spin.value() if self.toggle is None or self.toggle.isChecked() else -1

    def setValue(self, value: float) -> None:
        previous = self.value()
        with QSignalBlocker(self):
            if self.toggle is not None:
                with QSignalBlocker(self.toggle):
                    self.toggle.setChecked(value >= 0)
                self._sync_enabled()
            if value >= 0 or self.toggle is None:
                self.spin.setValue(value)
        if self.value() != previous:
            self.valueChanged.emit(self.value())

    def setAccessibleName(self, name: str) -> None:
        super().setAccessibleName(name)
        self.spin.setAccessibleName(name)
        self.slider.setAccessibleName(name + " — " + t("percent_quick_select"))
        if self.toggle is not None:
            self.toggle.setAccessibleName(name + " — " + t("percent_enabled"))

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self.spin.setToolTip(text)
        self.slider.setToolTip(text)
        if self.toggle is not None:
            self.toggle.setToolTip(t("percent_enabled") + "\n" + text)

    def _sync_enabled(self):
        enabled = self.toggle is None or self.toggle.isChecked()
        self.spin.setEnabled(enabled)
        self.slider.setEnabled(enabled)

    def _enabled_changed(self):
        self._sync_enabled()
        self.valueChanged.emit(self.value())

    def _spin_changed(self, value):
        with QSignalBlocker(self.slider):
            self.slider.setValue(round(value))
        self.valueChanged.emit(self.value())

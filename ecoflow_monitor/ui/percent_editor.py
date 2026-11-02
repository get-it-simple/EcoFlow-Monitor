from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QHBoxLayout, QSlider, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t


class PercentEditor(QWidget):
    valueChanged = Signal(float)

    def __init__(self, minimum=0, maximum=100, decimals=0, optional=False, default=0, compact=False, parent=None):
        super().__init__(parent)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(" %")
        self.spin.setValue(default)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(round(self.spin.value()))
        self.slider.setMinimumWidth(80)
        self.slider.setAccessibleName(t("percent_quick_select"))
        self.toggle = QCheckBox(t("percent_enabled")) if optional else None
        if self.toggle is not None:
            self.toggle.setToolTip(t("percent_enabled"))
            self.toggle.setAccessibleName(t("percent_enabled"))
            if compact:
                self.toggle.setText("")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QHBoxLayout()
        if self.toggle is not None:
            row.addWidget(self.toggle)
            self.toggle.toggled.connect(self._enabled_changed)
        row.addWidget(self.spin, 1)
        layout.addLayout(row)
        layout.addWidget(self.slider)
        self.setFocusProxy(self.spin)
        self.spin.valueChanged.connect(self._spin_changed)
        self.slider.valueChanged.connect(self.spin.setValue)
        self._sync_enabled()

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

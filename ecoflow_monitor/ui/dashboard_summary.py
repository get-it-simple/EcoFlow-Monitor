from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLayout, QSizePolicy, QWidget

from ecoflow_monitor.settings import DASHBOARD_SUMMARY_METRICS
from ecoflow_monitor.ui.components import StatusBadge
from ecoflow_monitor.ui.polling import telemetry_source_text
from ecoflow_monitor.ui.readings import MetricReading
from ecoflow_monitor.ui.tokens import SPACE, TOKENS


class DashboardSummary(QWidget):
    def __init__(self, controller, parent):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("dashboard_summary")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.readings = {key: MetricReading(controller, key, self) for key in DASHBOARD_SUMMARY_METRICS}
        for reading in self.readings.values():
            reading.setProperty("focus_target", True)
            reading.box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.status = StatusBadge(controller, self, retry_credentials=True)
        self.status.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.order = ()
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

    def refresh(self, scale, order, clear):
        if self.order != order:
            self.order = order
            while self.row.count():
                self.row.takeAt(0)
            for key in order:
                self.row.addWidget(self.readings[key])
            self.row.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
            items = [*(self.readings[key] for key in order), self.status]
            for first, second in zip(items, items[1:]):
                self.setTabOrder(first, second)
        self.row.setSpacing(round(SPACE[1] * scale))
        for reading in self.readings.values():
            reading.material = "Clear" if clear else "Panel"
            reading.configure(scale, TOKENS["widget"]["minimalValueSize"], clear, labels=False,
                              padding=TOKENS["widget"]["minimalPadding"], fixed_slot=True)
            reading.setFixedWidth(reading.baseline_size().width())
        source = self.controller.telemetry_source
        self.status.outlined = clear
        self.status.source = telemetry_source_text(source, compact=True)
        self.status.source_detail = telemetry_source_text(source)
        self.status.refresh()
        self.status.ensurePolished()

    def set_status_compact(self, compact):
        self.status.set_indicator_only(compact)
        width = self.status.sizeHint().width()
        self.status.setFixedWidth(width)
        margins = self.row.contentsMargins()
        self.setFixedWidth(self.reading_width() + width + margins.left() + margins.right())

    def reading_width(self):
        return sum(reading.minimumWidth() for reading in self.readings.values()) + self.row.spacing() * len(self.readings)

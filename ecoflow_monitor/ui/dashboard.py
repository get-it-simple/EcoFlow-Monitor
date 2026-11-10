from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from ecoflow_monitor import __version__
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, clamp_window, hint, number, status_tooltip, update_status
from ecoflow_monitor.ui.readings import EnergyBalance, MetricReading
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.tokens import COLORS


class Dashboard(SettingsWindow):
    def __init__(self, controller, open_main, open_mini):
        super().__init__(controller)
        self.setObjectName("dashboard")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.setWindowTitle(t("app_title"))
        self.open_main = open_main
        self.open_mini = open_mini
        self.navigation = QListWidget(self)
        self.navigation.setObjectName("navigation")
        self.navigation.setAccessibleName(t("navigation"))
        self.navigation.setFixedWidth(216)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.tabBar().hide()
        monitoring = self._monitoring()
        self.tabs.insertTab(0, monitoring, t("monitoring"))
        self.tabs.tabBar().moveTab(2, 3)
        self.page_keys = ("monitoring", "connection_settings", "appearance_settings", "integrations", "hotkeys_settings")
        for key in self.page_keys:
            self.navigation.addItem(t(key))
        self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self._page_changed)
        outer = self.layout()
        outer.removeWidget(self.tabs)
        footer_layout = outer.takeAt(outer.count() - 1).layout()
        self.footer = QWidget(self)
        self.footer.setLayout(footer_layout)
        header = QHBoxLayout()
        self.brand = QLabel(t("app_title"), self)
        self.brand.setObjectName("section_title")
        header.addWidget(self.brand, 1)
        self.version = QLabel(__version__ + " · DELTA 3 Max", self)
        self.version.setObjectName("secondary")
        header.addWidget(self.version)
        outer.insertLayout(0, header)
        body = QHBoxLayout()
        body.addWidget(self.navigation)
        body.addWidget(self.tabs, 1)
        outer.insertLayout(1, body, 1)
        outer.addWidget(self.footer)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(16)
        self.navigation.setCurrentRow(0)
        x, y, width, height = controller.settings.dashboard_geometry
        self.resize(width, height)
        if x != -1 or y != -1:
            self.move(x, y)
        clamp_window(self)
        self.setStyleSheet(settings_style())
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(500)
        self._geometry_timer.timeout.connect(self._save_geometry)
        controller.changed.connect(self.refresh_monitoring)
        self._last_language = controller.settings.language
        self.refresh_monitoring()

    def _monitoring(self):
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(24)
        self.heading = QLabel(t("monitoring_heading"), body)
        self.heading.setObjectName("page_title")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        status_row = QHBoxLayout()
        self.connection_status = QLabel(body)
        self.connection_status.setWordWrap(True)
        status_row.addWidget(self.connection_status, 1)
        status_row.addWidget(action(t("refresh"), self.controller.refresh))
        layout.addLayout(status_row)
        self.metric_grid = QGridLayout()
        self.metric_grid.setSpacing(12)
        self.metric_cards = []
        self.monitor_readings = {}
        for key in ("battery_level", "input_power", "output_power", "estimated_runtime"):
            card = QFrame(body)
            card.setObjectName("card")
            box = QVBoxLayout(card)
            box.setContentsMargins(0, 0, 0, 0)
            reading = MetricReading(self.controller, key, card)
            reading.configure(1, 28 if key == "estimated_runtime" else 36)
            box.addWidget(reading)
            self.monitor_readings[key] = reading
            self.metric_cards.append(card)
        layout.addLayout(self.metric_grid)
        self.balance_card = QFrame(body)
        self.balance_card.setObjectName("card")
        balance_layout = QVBoxLayout(self.balance_card)
        balance_layout.setContentsMargins(0, 0, 0, 0)
        self.balance_title = QLabel(t("energy_balance"), self.balance_card)
        self.balance_title.setObjectName("section_title")
        balance_layout.addWidget(self.balance_title)
        self.balance = EnergyBalance(self.controller, self.balance_card)
        balance_layout.addWidget(self.balance)
        self.balance_caption = QLabel(self.balance_card)
        self.balance_caption.setWordWrap(True)
        balance_layout.addWidget(self.balance_caption)
        layout.addWidget(self.balance_card)
        self.port_card = QFrame(body)
        self.port_card.setObjectName("card")
        self.port_grid = QGridLayout(self.port_card)
        self.port_grid.setContentsMargins(0, 0, 0, 0)
        self.port_readings = {}
        layout.addWidget(self.port_card)
        self.monitor_limits = hint("")
        layout.addWidget(self.monitor_limits)
        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(action(t("open_main_widget"), lambda: self.open_main()))
        buttons.addWidget(action(t("open_mini_widget"), lambda: self.open_mini()))
        layout.addLayout(buttons)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        return scroll

    def _page_changed(self, index):
        self.navigation.setCurrentRow(index)
        self.footer.setVisible(index != 0) if hasattr(self, "footer") else None
        self.error_label.setVisible(index != 0)
        self._connect_draft()

    def refresh_monitoring(self):
        update_status(self.connection_status, self.controller)
        self.connection_status.setText("DELTA 3 Max · " + status_tooltip(self.controller).replace("\n", " · "))
        for reading in self.monitor_readings.values():
            reading.refresh()
        telemetry = self.controller.telemetry
        ports = dict(telemetry.ports) if telemetry else {}
        if tuple(ports) != tuple(self.port_readings):
            while self.port_grid.count():
                self.port_grid.takeAt(0).widget().deleteLater()
            self.port_readings = {}
            for index, key in enumerate(ports):
                reading = MetricReading(self.controller, key, self.port_card)
                reading.configure(1, 24)
                self.port_readings[key] = reading
                self.port_grid.addWidget(reading, index // 2, index % 2)
        for reading in self.port_readings.values():
            reading.refresh()
        self.port_card.setVisible(bool(ports))
        self.balance.refresh()
        if telemetry:
            delta = telemetry.input_w - telemetry.output_w
            self.balance_caption.setText(t("power_balance").format(value=number(delta, t("unit_watt"))))
        else:
            self.balance_caption.setText(t("missing_data"))
        self.monitor_limits.setText(t("minimum_discharge_level") + ": " + number(self.controller.minimum.value, "%") + " · " + t("maximum_charge_level") + ": " + number(self.controller.maximum.value, "%"))
        if self._last_language != self.controller.settings.language:
            self._last_language = self.controller.settings.language
            for index, key in enumerate(self.page_keys):
                self.navigation.item(index).setText(t(key))
            self.heading.setText(t("monitoring_heading"))
            self.balance_title.setText(t("energy_balance"))

    def _fit_content(self):
        clamp_window(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "metric_grid"):
            return
        columns = 4 if self.width() >= 1120 else 2 if self.width() >= 740 else 1
        while self.metric_grid.count():
            self.metric_grid.takeAt(0)
        for index, card in enumerate(self.metric_cards):
            self.metric_grid.addWidget(card, index // columns, index % columns)
        if hasattr(self, "_geometry_timer"):
            self._geometry_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_geometry_timer"):
            self._geometry_timer.start()

    def _save_geometry(self):
        geometry = (self.x(), self.y(), self.width(), self.height())
        if geometry != self.controller.settings.dashboard_geometry:
            self.controller.update_preferences(dashboard_geometry=geometry)

    def reject(self):
        self.load_draft()
        self.error_label.clear()
        self._draft_changed()

    def closeEvent(self, event):
        self._draft_changed()
        if self._dirty:
            prompt = QMessageBox(self)
            prompt.setWindowTitle(t("unsaved_title"))
            prompt.setText(t("unsaved_message"))
            save = prompt.addButton(t("save"), QMessageBox.ButtonRole.AcceptRole)
            discard = prompt.addButton(t("discard"), QMessageBox.ButtonRole.DestructiveRole)
            cancel = prompt.addButton(t("cancel"), QMessageBox.ButtonRole.RejectRole)
            prompt.setDefaultButton(cancel)
            prompt.exec()
            selected = prompt.clickedButton()
            prompt.deleteLater()
            if selected is save:
                self.save()
                self._draft_changed()
                if self._dirty:
                    event.ignore()
                    return
            elif selected is not discard:
                event.ignore()
                return
        self.preview_changed.emit(None)
        self.load_draft()
        self._close_credentials()
        self._save_geometry()
        event.accept()

from datetime import datetime

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, effective_scale, hint, number, profile_tooltip
from ecoflow_monitor.ui.components import ActionLayout, SelectField as QComboBox, SettingRow, StatusBadge, Switch
from ecoflow_monitor.ui.limits import ChargeLimits
from ecoflow_monitor.ui.metrics import metric_label, metric_tooltip
from ecoflow_monitor.ui.notifications import NotificationIndicator
from ecoflow_monitor.ui.credential_renewal import CredentialRenewalBanner
from ecoflow_monitor.ui.readings import EnergyBalance, MetricReading
from ecoflow_monitor.ui.sections import CollapsibleSection
from ecoflow_monitor.ui.tokens import GEOMETRY, RADIUS, REFLOW_INTERVAL_MS
from ecoflow_monitor.ui.motion import MotionMessage, MotionSurface
from ecoflow_monitor.polling import PORT_KEYS
from ecoflow_monitor.ui.polling import LoadingIndicator


class MonitoringPage(QWidget):
    def __init__(self, controller, open_mini, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.hotkey_notice = ""
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(32, 24, 32, 32)
        self.box.setSpacing(24)
        self.heading_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        title_box = QVBoxLayout()
        self.updated = hint("")
        self.updated.setTextFormat(Qt.TextFormat.PlainText)
        title_box.addWidget(self.updated)
        self.heading_row.addLayout(title_box, 1)
        badges = QWidget(self)
        badge_row = QHBoxLayout(badges)
        badge_row.setContentsMargins(0, 0, 0, 0)
        self.status = StatusBadge(controller, self, retry_credentials=True)
        badge_row.addWidget(self.status)
        self.notifications = NotificationIndicator(self)
        self.notifications.setProperty("icon_button", True)
        badge_row.addWidget(self.notifications)
        self.heading_row.addWidget(badges, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.box.addLayout(self.heading_row)
        self.credential_renewal = CredentialRenewalBanner(controller, self)
        self.box.addWidget(self.credential_renewal)
        self.metric_grid = QGridLayout()
        self.metric_grid.setSpacing(8)
        self.cards = []
        self.readings = {}
        for key in ("battery_level", "input_power", "output_power", "estimated_runtime"):
            card = MotionSurface(controller, {"battery_level": "battery", "input_power": "input", "output_power": "output"}.get(key), self)
            card.setObjectName("card")
            box = QVBoxLayout(card)
            box.setContentsMargins(16, 16, 16, 16)
            reading = MetricReading(controller, key, card)
            reading.channel = None
            box.addWidget(reading)
            self.readings[key] = reading
            self.cards.append(card)
            if key == "battery_level":
                self.battery_progress = EnergyBalance(controller, card, battery=True)
                box.addWidget(self.battery_progress)
        self.box.addLayout(self.metric_grid)
        self.balance_card = QFrame(self)
        self.balance_card.setObjectName("balance_card")
        balance_box = QVBoxLayout(self.balance_card)
        balance_box.setContentsMargins(24, 20, 24, 24)
        balance_box.setSpacing(20)
        self.balance_title = QLabel(t("energy_balance"))
        self.balance_title.setObjectName("section_title")
        balance_box.addWidget(self.balance_title)
        row = QHBoxLayout()
        self.balance_readings = {}
        for key in ("input_power", "output_power"):
            reading = MetricReading(controller, key, self.balance_card)
            row.addWidget(reading, 1)
            self.balance_readings[key] = reading
        balance_box.addLayout(row)
        self.balance = EnergyBalance(controller, self.balance_card)
        self.balance.setMinimumHeight(16)
        balance_box.addWidget(self.balance)
        self.balance_caption = QLabel()
        self.balance_caption.setWordWrap(True)
        balance_box.addWidget(self.balance_caption)
        self.balance_message = MotionMessage(controller, self.balance_card)
        self.balance_message.setWordWrap(True)
        balance_box.addWidget(self.balance_message)
        self.box.addWidget(self.balance_card)
        self.port_sections = {}
        for group, title in (("inputs", "inputs"), ("ac", "ac_outlets"), ("other", "other_outputs")):
            card = QFrame(self)
            card.setObjectName("ports_card")
            box = QVBoxLayout(card)
            heading = QLabel(t(title), card)
            heading.setObjectName("section_title")
            box.addWidget(heading)
            grid = QGridLayout()
            box.addLayout(grid)
            note = hint(t("outputs_read_only"))
            note.setVisible(group != "inputs")
            box.addWidget(note)
            self.port_sections[group] = (card, heading, grid, note)
            self.box.addWidget(card)
        self.port_values = {}
        self._port_signature = None
        bottom = ActionLayout()
        self.limits = ChargeLimits(controller, self)
        bottom.addWidget(self.limits, 1)
        self.open_mini_button = action(t("open_mini_widget"), open_mini)
        bottom.addWidget(self.open_mini_button)
        self.box.addLayout(bottom)
        self.msi_section = CollapsibleSection(t("integrations"), self)
        self.integration_selector = QComboBox()
        self.integration_selector.setAccessibleName(t("integrations"))
        self.integration_selector.currentIndexChanged.connect(self.refresh_integrations)
        self.msi_section.content.addWidget(self.integration_selector)
        self.auto_toggle = Switch(t("automatic_mode"))
        self.auto_toggle.toggled.connect(self.set_auto)
        self.msi_section.content.addWidget(SettingRow(t("automatic_mode"), self.auto_toggle))
        profile_row = QHBoxLayout()
        self.profile_choice = QComboBox()
        self.profile_choice.setAccessibleName(t("integration_profiles"))
        self.profile_choice.currentIndexChanged.connect(lambda: self.profile_choice.setToolTip(self.profile_choice.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
        profile_row.addWidget(self.profile_choice, 1)
        self.apply_profile = action(t("integration_apply"), self.request_profile)
        profile_row.addWidget(self.apply_profile)
        self.msi_section.content.addLayout(profile_row)
        self.box.addWidget(self.msi_section)
        self.box.addStretch()
        self._columns = None
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(REFLOW_INTERVAL_MS)
        self._reflow_timer.timeout.connect(self.reflow)
        self.refresh()

    def refresh(self):
        settings = self.controller.ui_settings
        scale = getattr(self.window(), "_scale", effective_scale(self, settings.scale))
        telemetry = self.controller.telemetry
        self.updated.setText(self.controller.device_name + " · " + (t("last_update").format(timestamp=datetime.fromtimestamp(telemetry.received_utc).astimezone().strftime("%H:%M")) if telemetry else t("never_updated")))
        self.status.refresh()
        self.notifications.refresh(self.controller, self.hotkey_notice)
        for key, reading in self.readings.items():
            reading.configure(scale, 28 if key == "estimated_runtime" else 36, labels=settings.monitor_labels_visible, hover=settings.monitor_values_on_hover)
        for card in self.cards:
            card.motion_radius = RADIUS["card"] * scale
            card.layout().setContentsMargins(*(round(16 * scale),) * 4)
        self.balance_card.layout().setContentsMargins(*(round(24 * scale),) * 4)
        for reading in self.balance_readings.values():
            reading.configure(scale, 28)
        self.balance_title.setText(t("energy_balance"))
        for group, (card, title, grid, note) in self.port_sections.items():
            title.setText(t({"inputs": "inputs", "ac": "ac_outlets", "other": "other_outputs"}[group]))
            note.setText(t("outputs_read_only"))
            card.layout().setContentsMargins(*(round(16 * scale),) * 4)
            grid.setSpacing(round(16 * scale))
        telemetry = self.controller.telemetry
        self.balance.configure(scale)
        self.battery_progress.configure(scale)
        self.balance.refresh()
        self.battery_progress.refresh()
        self.balance_caption.setText(t("missing_data") if telemetry is None or telemetry.input_w is None or telemetry.output_w is None or not self.controller.fresh_for({"input_power", "output_power"}) else t("power_balance").format(value=number(telemetry.input_w - telemetry.output_w, t("unit_watt"))))
        self.balance_message.setText(t("motion_" + self.balance.motion.state.mode))
        ports = dict(telemetry.ports) if telemetry else {}
        anderson = tuple(key for key in ports if key.startswith("anderson_")) or ("anderson",)
        keys = (*PORT_KEYS, *anderson)
        for key in keys:
            if key not in self.port_values:
                value = QLabel()
                value.loader = LoadingIndicator(self.controller, value)
                value.setObjectName("secondary")
                group = "inputs" if key in {"ac_input", "pv_input"} else "ac" if key == "ac_output" else "other"
                row = MotionSurface(self.controller, {"ac_output": "ac", "dc_output": "dc"}.get(key), self.port_sections[group][0])
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                label = QLabel(metric_label(key), row)
                label.setWordWrap(True)
                line.addWidget(label, 1)
                line.addWidget(value)
                if key in {"ac_output", "dc_output"}:
                    state = Switch(metric_label(key))
                    state.setEnabled(False)
                    state.setProperty("read_only_output", True)
                    policy = state.sizePolicy()
                    policy.setRetainSizeWhenHidden(True)
                    state.setSizePolicy(policy)
                    line.addWidget(state)
                    row.output_state = state
                    row.state_loader = LoadingIndicator(self.controller, row, state)
                self.port_values[key] = (row, value)
        for key, (row, value) in self.port_values.items():
            row.motion_radius = RADIUS["control"] * scale
            row.setVisible(key in keys)
            value.setText(number(ports.get(key), t("unit_watt")))
            state = self.controller.reading_state(key)
            value.loader.present(state, scale)
            if state == "loading":
                value.setText("     ")
            tooltip = metric_tooltip(key, self.controller)
            value.setToolTip(tooltip + "\n" + t("sensor_" + state) if state != "ready" else tooltip)
            if key in {"ac_output", "dc_output"}:
                enabled = getattr(telemetry, "ac_enabled" if key == "ac_output" else "dc_enabled", None)
                outlet_state = self.controller.reading_state("ac_enabled" if key == "ac_output" else "dc_enabled")
                row.output_state.setVisible(enabled is not None and outlet_state != "loading")
                row.output_state.setChecked(enabled is True)
                outlet_tooltip = t("outputs_read_only") + " · " + t("output_on" if enabled is True else "output_off" if enabled is False else "missing_data")
                row.output_state.setToolTip(outlet_tooltip + (" · " + t("sensor_" + outlet_state) if outlet_state != "ready" else ""))
                row.state_loader.present(outlet_state, scale)
                if state != "loading" and outlet_state != "loading":
                    value.setText(value.text() + " · " + t("output_on" if enabled is True else "output_off" if enabled is False else "missing_data"))
        self.limits.refresh()
        self.open_mini_button.setText(t("open_mini_widget"))
        self.refresh_integrations()
        self.reflow()

    def reflow(self):
        scale = getattr(self.window(), "_scale", effective_scale(self, self.controller.ui_settings.scale))
        available = min(self.width(), self.parentWidget().width()) if self.parentWidget() is not None else self.width()
        width = max(1, available - 64 * scale)
        self.heading_row.setDirection(QBoxLayout.Direction.TopToBottom if width < GEOMETRY["form_breakpoint"] * scale else QBoxLayout.Direction.LeftToRight)
        columns = 4 if width >= 840 * scale else 2 if width >= 420 * scale else 1
        port_width = max(1, width - 32 * scale)
        cell_width = max(round(240 * scale), *(row.minimumSizeHint().width() for row, value in self.port_values.values()))
        port_columns = min(3, max(1, int((port_width + 16 * scale) // (cell_width + 16 * scale))))
        keys = tuple(key for key, (row, value) in self.port_values.items() if not row.isHidden())
        signature = (port_columns, keys, scale)
        if self._port_signature != signature:
            self._port_signature = signature
            for group, (card, title, grid, note) in self.port_sections.items():
                while grid.count():
                    grid.takeAt(0)
                group_keys = [key for key in keys if ("inputs" if key in {"ac_input", "pv_input"} else "ac" if key == "ac_output" else "other") == group]
                for index, key in enumerate(group_keys):
                    grid.addWidget(self.port_values[key][0], index // port_columns, index % port_columns)
                for index in range(3):
                    grid.setColumnStretch(index, 1 if index < min(port_columns, len(group_keys)) else 0)
        if columns == self._columns:
            return
        self._columns = columns
        while self.metric_grid.count():
            self.metric_grid.takeAt(0)
        for index, card in enumerate(self.cards):
            self.metric_grid.addWidget(card, index // columns, index % columns)

    def schedule_reflow(self):
        if not self._reflow_timer.isActive():
            self._reflow_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_reflow_timer"):
            self.schedule_reflow()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.schedule_reflow()
        return super().eventFilter(watched, event)

    def refresh_integrations(self):
        modules = tuple(module for module in self.controller.settings.integrations if module.enabled)
        self.msi_section.setVisible(bool(modules))
        choices = tuple((module.id, module.label) for module in modules)
        existing = tuple((self.integration_selector.itemData(index), self.integration_selector.itemText(index)) for index in range(self.integration_selector.count()))
        if choices != existing:
            selected = self.integration_selector.currentData()
            self.integration_selector.blockSignals(True)
            self.integration_selector.clear()
            for module in modules:
                self.integration_selector.addItem(module.label, module.id)
            self.integration_selector.setCurrentIndex(max(0, self.integration_selector.findData(selected)))
            self.integration_selector.blockSignals(False)
        identity = self.integration_selector.currentData()
        if not identity:
            return
        module = self.controller.module(identity)
        state = self.controller.integration_states[identity]
        self.auto_toggle.blockSignals(True)
        self.auto_toggle.setChecked(module.auto_enabled)
        self.auto_toggle.setToolTip(t(state.auto_reason))
        self.auto_toggle.blockSignals(False)
        items = tuple((profile.id, profile.label) for profile in module.profiles)
        old = tuple((self.profile_choice.itemData(index), self.profile_choice.itemText(index)) for index in range(self.profile_choice.count()))
        if items != old:
            selected = self.profile_choice.currentData()
            self.profile_choice.clear()
            for profile in module.profiles:
                self.profile_choice.addItem(profile.label, profile.id)
            self.profile_choice.setCurrentIndex(max(0, self.profile_choice.findData(selected)))
        for index, profile in enumerate(module.profiles):
            self.profile_choice.setItemData(index, profile_tooltip(profile), Qt.ItemDataRole.ToolTipRole)
        self.profile_choice.setToolTip(self.profile_choice.currentData(Qt.ItemDataRole.ToolTipRole) or "")
        self.apply_profile.setText(t("integration_retry") if state.result and not state.result.success else t("integration_apply"))
        self.apply_profile.setEnabled(state.observation.ready and not state.busy and bool(module.profiles))

    def set_auto(self, enabled):
        if identity := self.integration_selector.currentData():
            self.controller.set_auto(identity, enabled)

    def request_profile(self):
        identity, profile = self.integration_selector.currentData(), self.profile_choice.currentData()
        if identity and profile:
            self.controller.request_profile(identity, profile)

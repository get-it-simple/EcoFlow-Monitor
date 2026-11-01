from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.domain import ProfileDefinition
from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import HOTKEY_ACTIONS, METRICS, HotkeyBinding, SettingsError
from ecoflow_monitor.ui.common import action, clamp_window, effective_scale, hint, scaled_font
from ecoflow_monitor.ui.rules import RulesEditor, optional_percent


class SettingsWindow(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle(t("settings"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        scaled_font(self, effective_scale(self, controller.settings.scale))
        self.resize(850, 780)
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self._build_connection()
        self._build_msi()
        self._build_rules()
        self._build_appearance()
        self._build_hotkeys()
        self.msi_enabled.toggled.connect(self._msi_toggled)
        buttons = QHBoxLayout()
        buttons.addWidget(action(t("reset"), self.load_draft))
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        buttons.addWidget(action(t("save"), self.save))
        outer.addLayout(buttons)
        controller.devices_ready.connect(self._devices)
        self.load_draft()

    def _tab(self, title: str):
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, title)
        return layout

    def _build_connection(self):
        layout = self._tab(t("connection_settings"))
        form = QFormLayout()
        self.access = QLineEdit()
        self.secret = QLineEdit()
        self.access.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(t("access_key"), self.access)
        form.addRow(t("secret_key"), self.secret)
        layout.addLayout(form)
        layout.addWidget(hint(t("credentials_hint")))
        self.discover_button = action(t("load_devices"), self.discover)
        layout.addWidget(self.discover_button)
        self.devices = QComboBox()
        self.devices.setEditable(True)
        form2 = QFormLayout()
        form2.addRow(t("device"), self.devices)
        layout.addLayout(form2)
        layout.addWidget(hint(t("load_devices_hint")))
        self.capacity = QDoubleSpinBox()
        self.capacity.setRange(1, 1000000)
        self.capacity.setDecimals(0)
        self.minimum = optional_percent()
        self.maximum = optional_percent()
        form3 = QFormLayout()
        form3.addRow(t("capacity_wh"), self.capacity)
        form3.addRow(t("manual_min_label"), self.minimum)
        form3.addRow(t("manual_max_label"), self.maximum)
        layout.addLayout(form3)
        layout.addWidget(hint(t("capacity_hint")))
        layout.addWidget(hint(t("manual_limit_hint")))
        layout.addStretch()

    def _build_msi(self):
        layout = self._tab(t("msi_integration"))
        self.msi_enabled = QCheckBox(t("msi_enabled"))
        layout.addWidget(self.msi_enabled)
        self.msi_body = QWidget()
        body = QVBoxLayout(self.msi_body)
        self.msi_path = QLineEdit()
        path_row = QHBoxLayout()
        path_row.addWidget(self.msi_path)
        path_row.addWidget(action(t("browse"), self.browse_msi))
        body.addWidget(QLabel(t("msi_executable")))
        body.addLayout(path_row)
        self.gpus = QComboBox()
        body.addWidget(QLabel(t("gpu")))
        body.addWidget(self.gpus)
        self.profile_table = QTableWidget(5, 3)
        self.profile_table.setHorizontalHeaderLabels([t("rule_profile"), t("profile_label"), t("profile_power")])
        self.profile_table.horizontalHeader().setStretchLastSection(True)
        self.profile_table.setMinimumHeight(230)
        body.addWidget(self.profile_table)
        body.addWidget(hint(t("profile_description_hint")))
        layout.addWidget(self.msi_body)
        layout.addStretch()
        self.msi_enabled.toggled.connect(self.msi_body.setVisible)

    def _build_rules(self):
        layout = self._tab(t("rules_settings"))
        self.rules_hint = hint(t("rule_hint"))
        layout.addWidget(self.rules_hint)
        self.application_rules = RulesEditor(True)
        self.system_rules = RulesEditor(False)
        layout.addWidget(QLabel(t("application_rules")))
        layout.addWidget(self.application_rules)
        layout.addWidget(QLabel(t("system_rules")))
        layout.addWidget(self.system_rules)
        self.rules_tab_index = self.tabs.count() - 1

    def _build_appearance(self):
        layout = self._tab(t("appearance_settings"))
        form = QFormLayout()
        self.language = QComboBox()
        self.language.addItem(t("language_system"), "system")
        for code in i18n.get_available_languages():
            self.language.addItem(i18n.language_display_name(code, i18n.get_current_language()), code)
        form.addRow(t("language"), self.language)
        layout.addLayout(form)
        layout.addWidget(action(t("language_guide"), lambda: QMessageBox.information(self, t("language_guide"), t("language_guide_text"))))
        self.scale = QDoubleSpinBox()
        self.scale.setRange(50, 300)
        self.scale.setDecimals(0)
        form2 = QFormLayout()
        form2.addRow(t("scale"), self.scale)
        layout.addLayout(form2)
        self.transparency = QCheckBox(t("transparency"))
        self.animations = QCheckBox(t("animations"))
        self.visible_on_start = QCheckBox(t("visible_on_start"))
        self.mini_enabled = QCheckBox(t("mini_widget"))
        self.mini_above = QCheckBox(t("mini_above_taskbar"))
        for control in (self.transparency, self.animations, self.visible_on_start, self.mini_enabled, self.mini_above):
            layout.addWidget(control)
        self.mini_scale = QDoubleSpinBox()
        self.mini_scale.setRange(50, 300)
        self.mini_scale.setDecimals(0)
        self.mini_alpha = QDoubleSpinBox()
        self.mini_alpha.setRange(0, 100)
        self.mini_alpha.setDecimals(0)
        self.dock = QComboBox()
        for key in ("", "left", "right", "top", "bottom"):
            self.dock.addItem(t("dock_" + (key or "free")), key)
        form3 = QFormLayout()
        form3.addRow(t("mini_scale"), self.mini_scale)
        form3.addRow(t("mini_transparency"), self.mini_alpha)
        form3.addRow(t("docking"), self.dock)
        layout.addLayout(form3)
        layout.addWidget(QLabel(t("mini_metrics")))
        self.metrics = QListWidget()
        self.metrics.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.metrics.setMinimumHeight(180)
        layout.addWidget(self.metrics)

    def _build_hotkeys(self):
        layout = self._tab(t("hotkeys_settings"))
        layout.addWidget(hint(t("hotkey_hint")))
        self.hotkeys = QTableWidget(len(HOTKEY_ACTIONS), 3)
        self.hotkeys.setHorizontalHeaderLabels([t("rule_enabled"), t("profile_label"), t("hotkeys_settings")])
        self.hotkeys.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.hotkeys)

    def load_draft(self):
        settings = self.controller.settings
        self._baseline = settings
        self.access.clear()
        self.secret.clear()
        self.devices.clear()
        self.devices.addItem(settings.serial, settings.serial)
        self.capacity.setValue(settings.capacity_wh)
        self.minimum.setValue(-1 if settings.manual_min is None else settings.manual_min)
        self.maximum.setValue(-1 if settings.manual_max is None else settings.manual_max)
        self.msi_enabled.setChecked(settings.msi_enabled)
        self.msi_body.setVisible(settings.msi_enabled)
        self.msi_path.setText(settings.msi_executable)
        self.gpus.clear()
        self.gpus.addItem(t("gpu_auto"), "")
        ids = set(self.controller.observation.gpu_ids)
        if settings.gpu_id:
            ids.add(settings.gpu_id)
        for gpu in sorted(ids):
            self.gpus.addItem(gpu, gpu)
        self.gpus.setCurrentIndex(max(0, self.gpus.findData(settings.gpu_id)))
        for row, profile in enumerate(settings.profiles):
            slot = QTableWidgetItem(str(profile.slot))
            slot.setFlags(slot.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.profile_table.setItem(row, 0, slot)
            self.profile_table.setItem(row, 1, QTableWidgetItem(profile.label))
            self.profile_table.setItem(row, 2, QTableWidgetItem("" if profile.power_pct is None else str(profile.power_pct)))
        self.application_rules.set_rules(settings.application_rules)
        self.system_rules.set_rules(settings.system_rules)
        self.tabs.setTabVisible(self.rules_tab_index, settings.msi_enabled)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        self.scale.setValue(settings.scale * 100)
        self.transparency.setChecked(settings.transparency)
        self.animations.setChecked(settings.animations)
        self.visible_on_start.setChecked(settings.monitor_visible)
        self.mini_enabled.setChecked(settings.mini_enabled)
        self.mini_above.setChecked(settings.mini_above_taskbar)
        self.mini_scale.setValue(settings.mini_scale * 100)
        self.mini_alpha.setValue(settings.mini_transparency * 100)
        self.dock.setCurrentIndex(max(0, self.dock.findData(settings.mini_dock)))
        self.metrics.clear()
        for key in (*settings.mini_metrics, *(key for key in METRICS if key not in settings.mini_metrics)):
            item = QListWidgetItem(t(key), self.metrics)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in settings.mini_metrics else Qt.CheckState.Unchecked)
        for row, binding in enumerate(settings.hotkeys):
            enabled = QCheckBox()
            enabled.setChecked(binding.enabled)
            self.hotkeys.setCellWidget(row, 0, enabled)
            label = QTableWidgetItem(t(binding.action))
            label.setData(Qt.ItemDataRole.UserRole, binding.action)
            label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.hotkeys.setItem(row, 1, label)
            self.hotkeys.setItem(row, 2, QTableWidgetItem(binding.combo))
            self.hotkeys.setRowHidden(row, not settings.msi_enabled and binding.action not in {"toggle_monitor", "toggle_mini"})
        self.hotkeys.resizeColumnsToContents()
        self.hotkeys.resizeRowsToContents()
        self.profile_table.resizeRowsToContents()

    def _msi_toggled(self, enabled):
        self.tabs.setTabVisible(self.rules_tab_index, enabled)
        for row in range(self.hotkeys.rowCount()):
            item = self.hotkeys.item(row, 1)
            if item is not None:
                action_name = item.data(Qt.ItemDataRole.UserRole)
                self.hotkeys.setRowHidden(row, not enabled and action_name not in {"toggle_monitor", "toggle_mini"})

    def _entered_credentials(self):
        access, secret = self.access.text().strip(), self.secret.text().strip()
        if bool(access) != bool(secret):
            raise CredentialError("credentials_required")
        return Credentials(access, secret) if access else None

    def discover(self):
        try:
            credentials = self._entered_credentials()
        except CredentialError as error:
            QMessageBox.warning(self, t("error_title"), t(str(error)))
            return
        self.discover_button.setEnabled(False)
        self.controller.discover(credentials)

    def _devices(self, devices):
        self.discover_button.setEnabled(True)
        if not devices:
            QMessageBox.warning(self, t("error_title"), t(self.controller.notice or self.controller.connection.value))
            return
        self.devices.clear()
        for device in devices:
            self.devices.addItem(device.name, device.serial)
        index = self.devices.findData(self.controller.settings.serial)
        if index >= 0:
            self.devices.setCurrentIndex(index)

    def browse_msi(self):
        path, _ = QFileDialog.getOpenFileName(self, t("msi_executable"), "", t("application_files") + " (*.exe)")
        if path:
            self.msi_path.setText(path)

    def gather(self):
        current = self.controller.settings
        serial = self.devices.currentData() if self.devices.currentIndex() >= 0 and self.devices.currentText() == self.devices.itemText(self.devices.currentIndex()) else self.devices.currentText().strip()
        profiles = tuple(ProfileDefinition(row + 1, self.profile_table.item(row, 1).text(), float(self.profile_table.item(row, 2).text()) if self.profile_table.item(row, 2).text().strip() else None) for row in range(5))
        hotkeys = tuple(HotkeyBinding(self.hotkeys.item(row, 1).data(Qt.ItemDataRole.UserRole), self.hotkeys.item(row, 2).text().strip().lower(), self.hotkeys.cellWidget(row, 0).isChecked()) for row in range(self.hotkeys.rowCount()))
        msi_path = self.msi_path.text().strip()
        if msi_path:
            msi_path = local_executable(msi_path, allow_shortcut=False)
        draft = replace(
            self._baseline, serial=serial or "", capacity_wh=self.capacity.value(),
            manual_min=self.minimum.value() if self.minimum.value() >= 0 else None,
            manual_max=self.maximum.value() if self.maximum.value() >= 0 else None,
            msi_enabled=self.msi_enabled.isChecked(), msi_executable=msi_path, gpu_id=self.gpus.currentData() or "",
            profiles=profiles, application_rules=self.application_rules.rules(True), system_rules=self.system_rules.rules(True),
            hotkeys=hotkeys, language=self.language.currentData(), scale=self.scale.value() / 100,
            transparency=self.transparency.isChecked(), animations=self.animations.isChecked(),
            monitor_visible=self.visible_on_start.isChecked(), mini_enabled=self.mini_enabled.isChecked(),
            mini_above_taskbar=self.mini_above.isChecked(), mini_scale=self.mini_scale.value() / 100,
            mini_transparency=self.mini_alpha.value() / 100, mini_dock=self.dock.currentData(),
            mini_metrics=tuple(self.metrics.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.metrics.count()) if self.metrics.item(index).checkState() == Qt.CheckState.Checked),
        )
        changes = {name: getattr(draft, name) for name in draft.__dataclass_fields__ if getattr(draft, name) != getattr(self._baseline, name)}
        return replace(current, **changes)

    def save(self):
        try:
            self.controller.apply_settings(self.gather(), self._entered_credentials())
        except (ValueError, OSError, SettingsError, CredentialError) as error:
            key = str(error)
            QMessageBox.warning(self, t("error_title"), t(key) if t(key) != key else t("invalid_settings"))
            return
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        clamp_window(self)

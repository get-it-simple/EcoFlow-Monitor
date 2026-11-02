from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.integrations import hotkey_actions, hotkey_label
from ecoflow_monitor.ui.integrations import IntegrationsEditor
from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import METRICS, HotkeyBinding, SettingsError
from ecoflow_monitor.ui.common import action, clamp_window, effective_scale, hint, scaled_font
from ecoflow_monitor.ui.rules import optional_percent
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor
from ecoflow_monitor.ui.reference_style import toggle_switch_style
from ecoflow_monitor.ui.theme import SETTINGS_STYLE
from ecoflow_monitor.ui.sections import CollapsibleSection


class SettingsWindow(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.controller = controller
        self.setWindowTitle(t("settings"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        area = self.screen().availableGeometry()
        scale = min(effective_scale(self, controller.settings.scale), (area.width() - 40) / 850, (area.height() - 60) / 780)
        scaled_font(self, scale)
        self.setStyleSheet(self.styleSheet() + SETTINGS_STYLE)
        self.resize(round(850 * scale), round(780 * scale))
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self._build_connection()
        self.integrations_editor = IntegrationsEditor()
        self._tab(t("integrations")).addWidget(self.integrations_editor)
        self._build_appearance()
        self._build_hotkeys()
        self.integrations_editor.changed.connect(self._refresh_hotkeys)
        buttons = QHBoxLayout()
        buttons.addWidget(action(t("reset"), self.load_draft))
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        save_button = action(t("save"), self.save)
        save_button.setObjectName("primary")
        buttons.addWidget(save_button)
        outer.addLayout(buttons)
        controller.devices_ready.connect(self._devices)
        self.load_draft()

    def _tab(self, title: str):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
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

    def _build_appearance(self):
        layout = self._tab(t("appearance_settings"))
        self.appearance_sections = {}
        for key in ("appearance_general", "appearance_monitor", "appearance_mini", "appearance_metrics"):
            section = CollapsibleSection(t(key))
            self.appearance_sections[key] = section
            layout.addWidget(section)
        general = self.appearance_sections["appearance_general"].content
        monitor = self.appearance_sections["appearance_monitor"].content
        mini = self.appearance_sections["appearance_mini"].content
        metrics = self.appearance_sections["appearance_metrics"].content
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.language = QComboBox()
        self.language.addItem(t("language_system"), "system")
        for code in i18n.get_available_languages():
            self.language.addItem(i18n.language_display_name(code, i18n.get_current_language()), code)
        form.addRow(t("language"), self.language)
        general.addLayout(form)
        general.addWidget(action(t("language_guide"), lambda: QMessageBox.information(self, t("language_guide"), t("language_guide_text"))))
        self.scale = QDoubleSpinBox()
        self.scale.setRange(50, 300)
        self.scale.setDecimals(0)
        form2 = QFormLayout()
        form2.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form2.addRow(t("scale"), self.scale)
        general.addLayout(form2)
        self.transparency = QCheckBox(t("transparency"))
        self.background_transparency = QDoubleSpinBox()
        self.background_transparency.setRange(0, 100)
        self.background_transparency.setDecimals(2)
        self.background_transparency.setSuffix(" %")
        self.background_transparency.setAccessibleName(t("main_transparency"))
        self.transparency.toggled.connect(self.background_transparency.setEnabled)
        monitor.addWidget(self.transparency)
        background_form = QFormLayout()
        background_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        background_form.addRow(t("main_transparency"), self.background_transparency)
        monitor.addLayout(background_form)
        monitor.addWidget(hint(t("main_transparency_hint")))
        self.animations = QCheckBox(t("animations"))
        self.visible_on_start = QCheckBox(t("visible_on_start"))
        self.mini_enabled = QCheckBox(t("mini_widget"))
        self.mini_above = QCheckBox(t("mini_above_taskbar"))
        for control in (self.animations, self.visible_on_start):
            monitor.addWidget(control)
        for control in (self.mini_enabled, self.mini_above):
            mini.addWidget(control)
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
        form3.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form3.addRow(t("mini_scale"), self.mini_scale)
        form3.addRow(t("mini_transparency"), self.mini_alpha)
        form3.addRow(t("docking"), self.dock)
        mini.addLayout(form3)
        metrics.addWidget(hint(t("mini_metrics")))
        self.metrics = QListWidget()
        self.metrics.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.metrics.setMinimumHeight(180)
        metrics.addWidget(self.metrics)

    def _build_hotkeys(self):
        layout = self._tab(t("hotkeys_settings"))
        layout.addWidget(hint(t("hotkey_hint")))
        self.hotkey_body = QWidget()
        self.hotkey_layout = QVBoxLayout(self.hotkey_body)
        self.hotkey_layout.setContentsMargins(0, 0, 0, 0)
        self.hotkey_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.addWidget(self.hotkey_body)
        layout.addStretch()
        self.hotkey_rows = {}
        self.hotkey_sections = {}

    def _refresh_hotkeys(self):
        try:
            modules = self.integrations_editor.modules()
        except (ValueError, AttributeError):
            return
        values = {binding.action: binding for binding in self.controller.settings.hotkeys}
        values.update({name: HotkeyBinding(name, editor.combo(), enabled.isChecked()) for name, (field, editor, enabled) in self.hotkey_rows.items()})
        expanded = {key: section.header.isChecked() for key, section in self.hotkey_sections.items()}
        self._clear_hotkeys()
        self.hotkey_rows = {}
        self.hotkey_sections = {}
        enabled_modules = {module.id for module in modules if module.enabled}
        for module in modules:
            section = CollapsibleSection(module.label)
            section.set_expanded(expanded.get(module.id, False))
            section.setVisible(module.id in enabled_modules)
            self.hotkey_layout.addWidget(section)
            self.hotkey_sections[module.id] = section
        for name in hotkey_actions(modules):
            binding = values.get(name, HotkeyBinding(name))
            field = QWidget()
            row = QVBoxLayout(field)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            label = hint(hotkey_label(name, modules))
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setToolTip(label.text())
            row.addWidget(label)
            controls = QHBoxLayout()
            enabled = QCheckBox()
            enabled.setChecked(binding.enabled)
            enabled.setObjectName("bindingToggle")
            enabled.setStyleSheet(toggle_switch_style(enabled.objectName()))
            editor = HotkeyComboEditor(binding.combo)
            controls.addWidget(enabled)
            controls.addWidget(editor, 1)
            row.addLayout(controls)
            target = self.hotkey_sections[name.split(":")[1]].content if ":" in name else self.hotkey_layout
            if ":" not in name:
                target.insertWidget(0 if name == "toggle_monitor" else 1, field)
            else:
                target.addWidget(field)
            self.hotkey_rows[name] = (field, editor, enabled)
            field.setVisible(":" not in name or name.split(":")[1] in enabled_modules)

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
        self.integrations_editor.load(settings.integrations)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        self.scale.setValue(settings.scale * 100)
        self.transparency.setChecked(settings.transparency)
        self.background_transparency.setValue((1 - settings.background_opacity) * 100)
        self.background_transparency.setEnabled(settings.transparency)
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
        self.hotkey_rows = self._reset_hotkeys()

    def _reset_hotkeys(self):
        self._clear_hotkeys()
        self.hotkey_rows = {}
        self.hotkey_sections = {}
        self._refresh_hotkeys()
        return self.hotkey_rows

    def _clear_hotkeys(self):
        while self.hotkey_layout.count():
            item = self.hotkey_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

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

    def gather(self):
        current = self.controller.settings
        serial = self.devices.currentData() if self.devices.currentIndex() >= 0 and self.devices.currentText() == self.devices.itemText(self.devices.currentIndex()) else self.devices.currentText().strip()
        hotkeys = tuple(HotkeyBinding(action_name, editor.combo(), enabled.isChecked()) for action_name, (field, editor, enabled) in self.hotkey_rows.items())
        modules = self.integrations_editor.modules(True)
        baseline = {module.id: module for module in self._baseline.integrations}
        latest = {module.id: module for module in current.integrations}
        merged = []
        for module in modules:
            if module.id in baseline and module.id in latest:
                edits = {name: getattr(module, name) for name in module.__dataclass_fields__ if getattr(module, name) != getattr(baseline[module.id], name)}
                module = replace(latest[module.id], **edits)
            merged.append(module)
        merged.extend(module for module in current.integrations if module.id not in baseline and module.id not in {item.id for item in modules})
        draft = replace(
            self._baseline, serial=serial or "", capacity_wh=self.capacity.value(),
            manual_min=self.minimum.value() if self.minimum.value() >= 0 else None,
            manual_max=self.maximum.value() if self.maximum.value() >= 0 else None,
            integrations=tuple(merged),
            hotkeys=hotkeys, language=self.language.currentData(), scale=self.scale.value() / 100,
            transparency=self.transparency.isChecked(), animations=self.animations.isChecked(),
            background_opacity=self._baseline.background_opacity if self.background_transparency.value() == round((1 - self._baseline.background_opacity) * 100, 2) else 1 - self.background_transparency.value() / 100,
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

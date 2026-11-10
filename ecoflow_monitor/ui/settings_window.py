from dataclasses import replace

from PySide6.QtCore import QModelIndex, QPoint, QSize, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.integrations import hotkey_actions, hotkey_label
from ecoflow_monitor.ui.integrations import IntegrationsEditor
from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import METRICS, HotkeyBinding, SettingsError
from ecoflow_monitor.ui.common import action, effective_scale, hint, scaled_font
from ecoflow_monitor.ui.rules import optional_percent
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor
from ecoflow_monitor.ui.reference_style import toggle_switch_style
from ecoflow_monitor.ui.theme import bind_style, settings_style
from ecoflow_monitor.ui.percent_editor import PercentEditor
from ecoflow_monitor.ui.sections import CollapsibleSection
from ecoflow_monitor.ui.metrics import metric_tooltip
from ecoflow_monitor.ui.scrollbar import install_scrollbars
from ecoflow_monitor.ui.onepassword import OnePasswordPanel
from ecoflow_monitor.ui.focus import FocusBinding
from ecoflow_monitor.ui.localization import TranslationBinding


class SettingsWindow(QDialog):
    preview_changed = Signal(object)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.controller = controller
        self._loading = True
        self.preflight = None
        self._dirty = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(80)
        self._draft_timer.timeout.connect(self._draft_changed)
        self.setWindowTitle(t("settings"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        area = self.screen().availableGeometry()
        scale = min(effective_scale(self, controller.settings.scale), (area.width() - 40) / 850, (area.height() - 60) / 780)
        self._settings_scale = scale
        scaled_font(self, scale)
        bind_style(self, lambda accent: settings_style(accent, self._settings_scale))
        self.resize(round(850 * scale), round(780 * scale))
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self._build_connection()
        integrations = self._tab(t("integrations"))
        self.integrations_section = CollapsibleSection(t("integrations"))
        integrations.addWidget(self.integrations_section)
        self.integrations_editor = IntegrationsEditor(self.integrations_section.body)
        self.integrations_section.content.addWidget(self.integrations_editor)
        self._build_appearance()
        self._build_hotkeys()
        self.integrations_editor.changed.connect(self._refresh_hotkeys)
        buttons = QHBoxLayout()
        buttons.addWidget(action(t("reset"), self.load_draft))
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        save_button = action(t("save"), self.save)
        self.save_button = save_button
        save_button.setObjectName("primary")
        buttons.addWidget(save_button)
        outer.addLayout(buttons)
        controller.devices_ready.connect(self._devices)
        self.load_draft()
        controller.changed.connect(self._refresh_metric_tooltips)
        self.finished.connect(self._close_credentials)
        self.finished.connect(lambda: self.preview_changed.emit(None))
        self.error_label = QLabel(self)
        self.error_label.setObjectName("form_error")
        self.error_label.setWordWrap(True)
        self.error_label.setMinimumHeight(self.fontMetrics().height())
        outer.insertWidget(1, self.error_label)
        self._loading = False
        self._connect_draft()
        self._draft_changed()
        self._focus_binding = FocusBinding(self)
        self._translations = TranslationBinding(self)
        install_scrollbars(self)

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
        self.manual_credentials = QWidget(self)
        manual_layout = QVBoxLayout(self.manual_credentials)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.access = QLineEdit()
        self.secret = QLineEdit()
        self.access.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.access.setAccessibleName(t("access_key"))
        self.secret.setAccessibleName(t("secret_key"))
        form.addRow(t("access_key"), self.access)
        form.addRow(t("secret_key"), self.secret)
        manual_layout.addLayout(form)
        self.show_keys = QCheckBox(t("show_keys"))
        self.show_keys.toggled.connect(self._show_keys)
        manual_layout.addWidget(self.show_keys)
        manual_layout.addWidget(hint(t("credentials_hint")))
        self.onepassword = OnePasswordPanel(self._entered_credentials, self.manual_credentials, self)
        self.onepassword.source_changed.connect(self._credential_source_changed)
        self.onepassword.credentials_ready.connect(self._fill_credentials)
        self.onepassword.busy_changed.connect(self._credential_busy_changed)
        layout.addWidget(self.onepassword)
        self.discover_button = action(t("load_devices"), self.discover)
        layout.addWidget(self.discover_button)
        self.devices = QComboBox()
        self.devices.setEditable(True)
        form2 = QFormLayout()
        form2.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form2.addRow(t("device"), self.devices)
        layout.addLayout(form2)
        layout.addWidget(hint(t("load_devices_hint")))
        self.capacity = QDoubleSpinBox()
        self.capacity.setRange(1, 1000000)
        self.capacity.setDecimals(0)
        self.minimum = optional_percent()
        self.maximum = optional_percent(100)
        self.minimum.setAccessibleName(t("manual_min_label"))
        self.maximum.setAccessibleName(t("manual_max_label"))
        form3 = QFormLayout()
        form3.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form3.addRow(t("capacity_wh"), self.capacity)
        form3.addRow(t("manual_min_label"), self.minimum)
        form3.addRow(t("manual_max_label"), self.maximum)
        for widget, key in ((self.capacity, "capacity_wh"), (self.minimum, "minimum_discharge_level"), (self.maximum, "maximum_charge_level")):
            tooltip = metric_tooltip(key)
            if widget is not self.capacity:
                tooltip += "\n" + t("manual_limit_hint")
            widget.setToolTip(tooltip)
            form3.labelForField(widget).setToolTip(tooltip)
        layout.addLayout(form3)
        self.range_error = QLabel()
        self.range_error.setObjectName("form_error")
        self.range_error.setWordWrap(True)
        self.range_error.setMinimumHeight(self.fontMetrics().height())
        layout.addWidget(self.range_error)
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
        self.appearance_sections["appearance_general"].set_expanded(True)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.language = QComboBox()
        self.language.addItem(t("language_system"), "system")
        for code in i18n.get_available_languages():
            self.language.addItem(i18n.language_display_name(code, i18n.get_current_language()), code)
        form.addRow(t("language"), self.language)
        general.addLayout(form)
        general.addWidget(action(t("language_guide"), lambda: QMessageBox.information(self, t("language_guide"), t("language_guide_text"))))
        self.scale = PercentEditor(50, 300)
        self.scale.setAccessibleName(t("scale"))
        form2 = QFormLayout()
        form2.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form2.addRow(t("scale"), self.scale)
        general.addLayout(form2)
        self.transparency = QCheckBox(t("transparency"))
        self.background_transparency = PercentEditor(decimals=2)
        self.background_transparency.setAccessibleName(t("main_transparency"))
        self.transparency.hide()
        self.background_transparency.valueChanged.connect(lambda: self._opacity_changed("main"))
        background_form = QFormLayout()
        background_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        background_form.addRow(t("main_transparency"), self.background_transparency)
        monitor.addLayout(background_form)
        monitor.addWidget(hint(t("main_transparency_hint")))
        self.animations = QCheckBox(t("animations"))
        self.visible_on_start = QCheckBox(t("visible_on_start"))
        self.monitor_labels_visible = QCheckBox(t("monitor_labels_visible"))
        self.monitor_values_on_hover = QCheckBox(t("monitor_values_on_hover"))
        self.mini_enabled = QCheckBox(t("mini_widget"))
        self.mini_above = QCheckBox(t("mini_above_taskbar"))
        for control in (self.animations, self.visible_on_start, self.monitor_labels_visible, self.monitor_values_on_hover):
            monitor.addWidget(control)
        for control in (self.mini_enabled, self.mini_above):
            mini.addWidget(control)
        self.overlay_fields = {}
        self.materials = {}
        for kind, target in (("main", monitor), ("mini", mini)):
            material = QComboBox()
            for name in ("panel", "overlay", "clear"):
                material.addItem(t("material_" + name), name)
            material.setAccessibleName(t("widget_material"))
            row = QFormLayout()
            row.addRow(t("widget_material"), material)
            target.addLayout(row)
            self.materials[kind] = material
            material.currentIndexChanged.connect(lambda index, name=kind: self._material_changed(name))
            clear_hint = hint(t("clear_readability"))
            target.addWidget(clear_hint)
            target.addWidget(action(t("restore_overlay"), lambda checked=False, name=kind: self._restore_overlay(name)))
            fields = {}
            for name, label in (("pinned", "pin_widget"), ("position_locked", "position_lock"), ("click_through", "click_through")):
                control = QCheckBox(t(label))
                control.setAccessibleName(t(label))
                target.addWidget(control)
                fields[name] = control
            self.overlay_fields[kind] = fields
        monitor.addWidget(hint(t("click_through_hint")))
        self.mini_scale = PercentEditor(50, 300)
        self.mini_alpha = PercentEditor()
        self.mini_alpha.valueChanged.connect(lambda: self._opacity_changed("mini"))
        self.mini_scale.setAccessibleName(t("mini_scale"))
        self.mini_alpha.setAccessibleName(t("mini_transparency"))
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
        metric_buttons = QHBoxLayout()
        metric_buttons.addWidget(action(t("move_up"), lambda: self._move_metric(-1)))
        metric_buttons.addWidget(action(t("move_down"), lambda: self._move_metric(1)))
        metrics.addLayout(metric_buttons)

    def _restore_overlay(self, kind):
        self.materials[kind].setCurrentIndex(1)
        self._material_changed(kind)

    def _material_changed(self, kind):
        if self._loading:
            return
        material = self.materials[kind].currentData()
        value = {"panel": 100, "overlay": 82, "clear": 0}[material]
        if kind == "main":
            self.transparency.setChecked(material != "panel")
            self.background_transparency.setValue(value)
        else:
            self.mini_alpha.setValue(value)

    def _opacity_changed(self, kind):
        if self._loading:
            return
        value = self.background_transparency.value() if kind == "main" else self.mini_alpha.value()
        if kind == "main":
            self.transparency.setChecked(value != 100)
        with QSignalBlocker(self.materials[kind]):
            self.materials[kind].setCurrentIndex(0 if value == 100 else 2 if value == 0 else 1)

    def _move_metric(self, direction):
        row = self.metrics.currentRow()
        target = row + direction
        if row < 0 or not 0 <= target < self.metrics.count():
            return
        self.metrics.model().moveRows(QModelIndex(), row, 1, QModelIndex(), target if direction < 0 else target + 1)
        self.metrics.setCurrentRow(target)
        self._draft_timer.start()

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
            section = CollapsibleSection(module.label, self.hotkey_body)
            self.hotkey_layout.addWidget(section)
            section.set_expanded(expanded.get(module.id, False))
            section.setVisible(module.id in enabled_modules)
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
            bind_style(enabled, lambda color, name=enabled.objectName(): toggle_switch_style(name, color))
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
        if not self._loading:
            self._connect_draft()

    def load_draft(self):
        was_loading = self._loading
        self._loading = True
        settings = self.controller.settings
        self._baseline = settings
        self.show_keys.setChecked(False)
        self.onepassword.load(settings)
        self.access.clear()
        self.secret.clear()
        self.devices.clear()
        self.devices.addItem(settings.serial, settings.serial)
        self.capacity.setValue(settings.capacity_wh)
        self.minimum.setValue(-1 if settings.manual_min is None else settings.manual_min)
        self.maximum.setValue(-1 if settings.manual_max is None else settings.manual_max)
        if settings.manual_min is None:
            self.minimum.spin.setValue(settings.manual_min_retained)
        if settings.manual_max is None:
            self.maximum.spin.setValue(settings.manual_max_retained)
        self.integrations_editor.load(settings.integrations)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        self.scale.setValue(settings.scale * 100)
        self.transparency.setChecked(settings.transparency)
        self.background_transparency.setValue(settings.background_opacity * 100 if settings.transparency else 100)
        self.animations.setChecked(settings.animations)
        self.visible_on_start.setChecked(settings.monitor_visible)
        self.monitor_labels_visible.setChecked(settings.monitor_labels_visible)
        self.monitor_values_on_hover.setChecked(settings.monitor_values_on_hover)
        self.mini_enabled.setChecked(settings.mini_enabled)
        self.mini_above.setChecked(settings.mini_above_taskbar)
        self.mini_scale.setValue(settings.mini_scale * 100)
        self.mini_alpha.setValue((1 - settings.mini_transparency) * 100)
        for kind in ("main", "mini"):
            preferences = getattr(settings, kind + "_overlay")
            for name, control in self.overlay_fields[kind].items():
                control.setChecked(getattr(preferences, name))
            opacity = settings.background_opacity if kind == "main" and settings.transparency else 1 if kind == "main" else 1 - settings.mini_transparency
            self.materials[kind].setCurrentIndex(0 if opacity == 1 else 2 if opacity == 0 else 1)
        self.dock.setCurrentIndex(max(0, self.dock.findData(settings.mini_dock)))
        self.metrics.clear()
        selected_metrics = settings.mini_metrics + settings.mini_hidden_metrics
        for key in settings.metric_order:
            item = QListWidgetItem(self.metrics)
            item.setToolTip(metric_tooltip(key, self.controller))
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, t(key))
            row = QWidget()
            row.setToolTip(item.toolTip())
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(QLabel(t(key)), 1)
            mode = QComboBox()
            mode.setAccessibleName(t(key))
            for name in ("always", "expanded", "off"):
                mode.addItem(t("metric_mode_" + name), name)
            mode.setCurrentIndex(0 if key in settings.mini_metrics else 1 if key in settings.mini_hidden_metrics else 2)
            item.setData(Qt.ItemDataRole.UserRole + 1, mode.currentData())
            mode.currentIndexChanged.connect(lambda index, entry=item, choice=mode: entry.setData(Qt.ItemDataRole.UserRole + 1, choice.itemData(index)))
            row_layout.addWidget(mode)
            item.setSizeHint(row.sizeHint())
            self.metrics.setItemWidget(item, row)
        self.hotkey_rows = self._reset_hotkeys()
        self._loading = was_loading
        if not self._loading:
            self.preview_changed.emit(None)
            self._connect_draft()
            self._draft_timer.start()

    def _reset_hotkeys(self):
        self._clear_hotkeys()
        self.hotkey_rows = {}
        self.hotkey_sections = {}
        self._refresh_hotkeys()
        return self.hotkey_rows

    def _refresh_metric_tooltips(self):
        for index in range(self.metrics.count()):
            item = self.metrics.item(index)
            item.setToolTip(metric_tooltip(item.data(Qt.ItemDataRole.UserRole), self.controller))
            self.metrics.itemWidget(item).setToolTip(item.toolTip())

    def _clear_hotkeys(self):
        while self.hotkey_layout.count():
            item = self.hotkey_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _entered_credentials(self):
        if self.onepassword.uses_onepassword:
            return self.onepassword.credentials
        access, secret = self.access.text().strip(), self.secret.text().strip()
        if bool(access) != bool(secret):
            raise CredentialError("credentials_required")
        return Credentials(access, secret) if access else None

    def _credential_source_changed(self):
        self.show_keys.setChecked(False)
        self.access.clear()
        self.secret.clear()
        self.access.setReadOnly(self.onepassword.uses_onepassword)
        self.secret.setReadOnly(self.onepassword.uses_onepassword)
        self.manual_credentials.setVisible(not self.onepassword.uses_onepassword)

    def _show_keys(self, visible):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.access.setEchoMode(mode)
        self.secret.setEchoMode(mode)

    def _fill_credentials(self, credentials):
        if credentials is not None:
            self.access.setText(credentials.access_key)
            self.secret.setText(credentials.secret_key)

    def _credential_busy_changed(self, busy):
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(not busy and self._dirty)
        if hasattr(self, "discover_button"):
            self.discover_button.setEnabled(not busy)
        if not busy and hasattr(self, "error_label"):
            self._draft_timer.stop()
            self._draft_changed()

    def _close_credentials(self):
        self.onepassword.cancel(show_status=False)
        self.onepassword.credentials = None
        self.access.clear()
        self.secret.clear()

    def discover(self):
        if self.onepassword.busy:
            return
        if self.onepassword.uses_onepassword and self.onepassword.credentials is None:
            self.onepassword.show_status(t("onepassword_load_first"))
            return
        try:
            credentials = self._entered_credentials()
        except CredentialError as error:
            QMessageBox.warning(self, t("error_title"), t(str(error)))
            return
        self.discover_button.setEnabled(False)
        self.controller.discover(credentials)

    def _devices(self, devices):
        self.discover_button.setEnabled(not self.onepassword.busy)
        if not devices:
            QMessageBox.warning(self, t("error_title"), t(self.controller.notice or self.controller.connection.value))
            return
        self.devices.clear()
        for device in devices:
            self.devices.addItem(device.name, device.serial)
        index = self.devices.findData(self.controller.settings.serial)
        if index >= 0:
            self.devices.setCurrentIndex(index)

    def gather(self, validate_paths=True):
        current = self.controller.settings
        serial = self.devices.currentData() if self.devices.currentIndex() >= 0 and self.devices.currentText() == self.devices.itemText(self.devices.currentIndex()) else self.devices.currentText().strip()
        hotkeys = tuple(HotkeyBinding(action_name, editor.combo(), enabled.isChecked()) for action_name, (field, editor, enabled) in self.hotkey_rows.items())
        previous_actions = {binding.action for binding in self._baseline.hotkeys}
        hotkeys = tuple(binding for binding in hotkeys if binding.action in previous_actions or binding.combo or binding.enabled)
        modules = self.integrations_editor.modules(validate_paths)
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
            **self.onepassword.settings_fields(),
            manual_min=self.minimum.value() if self.minimum.value() >= 0 else None,
            manual_max=self.maximum.value() if self.maximum.value() >= 0 else None,
            integrations=tuple(merged),
            hotkeys=hotkeys, language=self.language.currentData(), scale=self.scale.value() / 100,
            transparency=self.transparency.isChecked(), animations=self.animations.isChecked(),
            background_opacity=self._baseline.background_opacity if not self.transparency.isChecked() or self.background_transparency.value() == round(self._baseline.background_opacity * 100, 2) else self.background_transparency.value() / 100,
            monitor_visible=self.visible_on_start.isChecked(), mini_enabled=self.mini_enabled.isChecked(),
            monitor_labels_visible=self.monitor_labels_visible.isChecked(),
            monitor_values_on_hover=self.monitor_values_on_hover.isChecked(),
            mini_above_taskbar=self.mini_above.isChecked(), mini_scale=self.mini_scale.value() / 100,
            mini_transparency=self._baseline.mini_transparency if self.mini_alpha.value() == round((1 - self._baseline.mini_transparency) * 100) else 1 - self.mini_alpha.value() / 100, mini_dock=self.dock.currentData(),
            mini_metrics=self._metric_group("always"),
            mini_hidden_metrics=self._metric_group("expanded"),
            metric_order=tuple(self.metrics.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.metrics.count())),
            manual_min_retained=self.minimum.spin.value(), manual_max_retained=self.maximum.spin.value(),
            **{kind + "_overlay": self._overlay_draft(kind) for kind in ("main", "mini")},
        )
        changes = {name: getattr(draft, name) for name in draft.__dataclass_fields__ if getattr(draft, name) != getattr(self._baseline, name)}
        return replace(current, **changes)

    def _overlay_draft(self, kind):
        baseline = getattr(self._baseline, kind + "_overlay")
        current = getattr(self.controller.settings, kind + "_overlay")
        changes = {name: control.isChecked() for name, control in self.overlay_fields[kind].items() if control.isChecked() != getattr(baseline, name)}
        return replace(current, **changes)

    def _connect_draft(self):
        for widget in self.findChildren(QWidget):
            if widget.property("draft_connected"):
                continue
            widget.setProperty("draft_connected", True)
            for name in ("textChanged", "valueChanged", "currentIndexChanged", "toggled", "changed", "comboChanged", "itemChanged"):
                signal = getattr(widget, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(self._schedule_draft)
        if not self.metrics.model().property("draft_connected"):
            self.metrics.model().setProperty("draft_connected", True)
            self.metrics.model().rowsMoved.connect(self._schedule_draft)
            self.metrics.model().dataChanged.connect(self._schedule_draft)

    def _schedule_draft(self, *args):
        if not self._loading:
            self._draft_timer.start()

    def _draft_changed(self):
        if self._loading:
            return
        invalid_range = self.minimum.value() >= 0 and self.maximum.value() >= 0 and self.minimum.value() >= self.maximum.value()
        self.range_error.setText(t("invalid_settings") if invalid_range else "")
        try:
            draft = self.gather(False)
            draft.validate()
            self._entered_credentials()
            self._dirty = draft != self.controller.settings or bool(self.access.text().strip() or self.secret.text().strip())
            recording = any(editor._recording for _, editor, _ in self.hotkey_rows.values())
            self.save_button.setEnabled(self._dirty and not self.onepassword.busy and not recording)
            self.error_label.clear()
            if self._dirty:
                self.preview_changed.emit(draft)
            else:
                self.preview_changed.emit(None)
        except (ValueError, OSError, SettingsError, CredentialError):
            self._dirty = True
            self.save_button.setEnabled(False)
            self.error_label.setText(t("invalid_settings"))

    def _metric_group(self, mode):
        return tuple(self.metrics.item(index).data(Qt.ItemDataRole.UserRole)
                     for index in range(self.metrics.count())
                     if self.metrics.item(index).data(Qt.ItemDataRole.UserRole + 1) == mode)

    def save(self):
        if self.onepassword.busy or any(editor._recording for _, editor, _ in self.hotkey_rows.values()):
            return
        rollback = None
        try:
            draft = self.gather()
            if self.preflight is not None:
                rollback = self.preflight(draft)
            self.controller.apply_settings(draft, self._entered_credentials())
        except (ValueError, OSError, SettingsError, CredentialError) as error:
            if rollback is not None:
                rollback()
            key = str(error)
            QMessageBox.warning(self, t("error_title"), t(key) if t(key) != key else t("invalid_settings"))
            return
        self.preview_changed.emit(None)
        self._translations.refresh()
        self.load_draft()
        self.error_label.setText(t("settings_saved"))

    def sync_runtime_overlays(self):
        self._loading = True
        for kind in ("main", "mini"):
            baseline = getattr(self._baseline, kind + "_overlay")
            current = getattr(self.controller.settings, kind + "_overlay")
            for name, control in self.overlay_fields[kind].items():
                if control.isChecked() == getattr(baseline, name):
                    control.setChecked(getattr(current, name))
            self._baseline = replace(self._baseline, **{kind + "_overlay": current})
        self._loading = False

    def showEvent(self, event):
        super().showEvent(event)
        self.integrations_section.set_expanded(False)
        self._fit_content()

    def _fit_content(self):
        area = self.screen().availableGeometry()
        frame = self.frameGeometry().size() - self.size()
        available = area.size() - frame
        self.ensurePolished()
        self.layout().activate()
        page = self.tabs.currentWidget()
        chrome = self.size() - page.viewport().size()
        bodies = [self.tabs.widget(index).widget() for index in range(self.tabs.count())]
        for body in bodies:
            body.ensurePolished()
            body.layout().activate()
        width = min(available.width(), max(self.width(), max(body.minimumSizeHint().width() for body in bodies) + chrome.width()))
        content_width = max(1, width - chrome.width())
        height = 0
        for body in bodies:
            layout = body.layout()
            preferred = layout.totalHeightForWidth(content_width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
            height = max(height, preferred, body.minimumSizeHint().height())
        self.resize(QSize(width, max(self.height(), height + chrome.height())).boundedTo(available))
        bounds = self.frameGeometry()
        x = min(max(bounds.left(), area.left()), area.right() - bounds.width() + 1)
        y = min(max(bounds.top(), area.top()), area.bottom() - bounds.height() + 1)
        self.move(self.pos() + QPoint(x - bounds.left(), y - bounds.top()))

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.i18n import t
from ecoflow_monitor.integrations import CliCommand, IntegrationDefinition, IntegrationProfile, Reaction, template_tokens
from ecoflow_monitor.ui.common import ActionButton, action, clamp_window, hint, profile_tooltip
from ecoflow_monitor.ui.sections import CollapsibleSection
from ecoflow_monitor.ui.components import ActionLayout, ContentTabs, DataTable, DesignDialog, FormLayout as QFormLayout, NumberField, SelectField as QComboBox, SettingRow, Switch, TextArea
from ecoflow_monitor.ui.integration_creation import IntegrationCreationDialog
from ecoflow_monitor.ui.integration_tokens import ProfileTokenDialog
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.tokens import COLORS
from ecoflow_monitor.ui.rules import RulesEditor
from ecoflow_monitor.ui.theme import bind_style, settings_style
from ecoflow_monitor.ui.metrics import metric_tooltip, rule_tooltip
from ecoflow_monitor.ui.scrollbar import install_scrollbars
from ecoflow_monitor.ui.dialogs import confirm


class ExecutableField(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def choose(self, path):
        self.setText(local_executable(path))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and len(event.mimeData().urls()) == 1:
            event.acceptProposedAction()

    def dropEvent(self, event):
        try:
            self.choose(event.mimeData().urls()[0].toLocalFile())
            event.acceptProposedAction()
        except (ValueError, OSError):
            QMessageBox.warning(self, t("error_title"), t("invalid_executable"))


class CommandDialog(DesignDialog):
    def __init__(self, module, command=None, parent=None):
        super().__init__(parent)
        self.module = module
        self.original = command
        self.command = command
        self.setObjectName("settings_dialog")
        bind_style(self, settings_style)
        self.setWindowTitle(t("integration_mapping"))
        self.resize(620, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        form = QFormLayout()
        self.identity = QLineEdit(command.id if command else "command_" + uuid.uuid4().hex[:8])
        self.identity.setReadOnly(True)
        self.label = QLineEdit(command.label if command else "")
        self.arguments = TextArea(subprocess.list2cmdline(command.arguments) if command else "")
        self.arguments.setProperty("code", True)
        form.addRow(t("profile_label"), self.label)
        form.addRow(t("integration_arguments"), self.arguments)
        layout.addLayout(form)
        advanced = CollapsibleSection(t("integration_advanced"))
        advanced.content.addWidget(self.identity)
        layout.addWidget(advanced)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.preview)
        self.error = QLabel()
        self.error.setObjectName("form_error")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(action(t("cancel"), self.reject))
        self.save_button = action(t("save"), self.save)
        self.save_button.setObjectName("primary")
        row.addWidget(self.save_button)
        self.set_footer(row)
        self.label.textChanged.connect(self.validate_draft)
        self.arguments.textChanged.connect(self.validate_draft)
        self.validate_draft()

    def draft(self):
        arguments = template_tokens("{app} " + self.arguments.toPlainText())[1:]
        command = CliCommand(self.identity.text(), self.label.text().strip(), arguments)
        mapping = tuple(item for item in self.module.mapping if item.id != command.id) + (command,)
        replace(self.module, mapping=mapping).validate()
        return command

    def validate_draft(self):
        try:
            command = self.draft()
            self.preview.setText(subprocess.list2cmdline((self.module.executable or t("integration_app_token"), *command.arguments)))
            self.error.clear()
            self.save_button.setEnabled(command != self.original)
        except ValueError:
            self.error.setText(t("invalid_settings"))
            self.save_button.setEnabled(False)

    def save(self):
        try:
            self.command = self.draft()
        except ValueError:
            self.error.setText(t("invalid_settings"))
            return
        self.accept()


class ProfileDialog(DesignDialog):
    def __init__(self, module: IntegrationDefinition, profile: IntegrationProfile | None = None, parent=None):
        super().__init__(parent)
        self.module = module
        self.setObjectName("settings_dialog")
        bind_style(self, settings_style)
        self.setWindowTitle(t("integration_profile"))
        self.resize(984, 900)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(24)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.identity = QLineEdit(profile.id if profile else uuid.uuid4().hex)
        self.identity.setReadOnly(profile is not None)
        self.label = QLineEdit(profile.label if profile else "")
        self.slot = NumberField()
        self.slot.setDecimals(0)
        self.slot.setRange(1, 5)
        self.slot.setValue(profile.slot if profile and profile.slot else 1)
        self.technical_details = TextArea(profile.technical_details if profile else "")
        self.technical_details.setPlaceholderText(t("profile_technical_details_hint"))
        self.technical_details.setProperty("wide_field", "long")
        form.addRow(t("name"), self.label)
        if module.driver == "afterburner":
            form.addRow(t("profile_number"), self.slot)
        form.addRow(t("profile_technical_details"), self.technical_details)
        layout.addLayout(form)
        advanced = CollapsibleSection(t("profile_technical_id"))
        advanced_form = QFormLayout()
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        advanced.content.addLayout(advanced_form)
        advanced_form.addRow(t("integration_id"), self.identity)
        layout.addWidget(advanced)
        self.reactions = {}
        for name in ("activation", "deactivation"):
            reaction = getattr(profile, name) if profile else Reaction()
            toggle = QCheckBox(t(name))
            toggle.setChecked(reaction.enabled)
            editor = TextArea("\n".join(reaction.commands))
            editor.setProperty("code", True)
            editor.setProperty("wide_field", "long")
            editor.setPlaceholderText("{app} {cli:command_id}")
            tools = QWidget()
            tools_layout = QVBoxLayout(tools)
            tools_layout.setContentsMargins(0, 0, 0, 0)
            row = ActionLayout()
            tokens = ("{app}",) + tuple("{cli:" + command.id + "}" for command in module.mapping)
            for token in tokens[:2]:
                chip = action(token, lambda checked=False, field=editor, value=token: field.insertPlainText(value))
                chip.setProperty("token_chip", True)
                row.addWidget(chip)
            row.addStretch()
            insert = ActionButton(t("integration_insert"))
            insert.clicked.connect(lambda checked=False, field=editor, button=insert: self.insert_token(field, button))
            row.addWidget(insert)
            tools_layout.addLayout(row)
            preview = QLabel()
            preview.setWordWrap(True)
            preview.setTextFormat(Qt.TextFormat.PlainText)
            def update_preview(field=editor, output=preview):
                try:
                    output.setText("\n".join(subprocess.list2cmdline(module.expand(line, module.executable or t("integration_app_token"), preview=True)) for line in field.toPlainText().splitlines() if line.strip()))
                except ValueError:
                    output.setText(t("invalid_template"))
            editor.textChanged.connect(update_preview)
            update_preview()
            tools_layout.addWidget(preview)
            editor.setEnabled(reaction.enabled)
            toggle.toggled.connect(editor.setEnabled)
            layout.addWidget(SettingRow(toggle, editor))
            tools.setProperty("wide_field", "long")
            tools_row = SettingRow("", tools)
            layout.addWidget(tools_row)
            tools_row.setVisible(reaction.enabled)
            toggle.toggled.connect(tools_row.setVisible)
            self.reactions[name] = (toggle, editor)
        layout.addWidget(hint(t("integration_reaction_hint")))
        buttons = ActionLayout()
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        save_button = action(t("save"), self.save)
        self.save_button = save_button
        save_button.setObjectName("primary")
        buttons.addWidget(save_button)
        self.set_footer(buttons)
        self.profile: IntegrationProfile | None = profile
        self.error_label = QLabel()
        self.error_label.setObjectName("form_error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addStretch()
        for widget in self.findChildren(QWidget):
            for signal_name in ("textChanged", "toggled", "valueChanged"):
                signal = getattr(widget, signal_name, None)
                if signal is not None:
                    signal.connect(self.validate_draft)
        self.validate_draft()
        install_scrollbars(self)

    def insert_token(self, field: TextArea, button: ActionButton):
        cursor = field.textCursor()
        dialog = ProfileTokenDialog(self.module, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        token = dialog.token
        dialog.deleteLater()
        if not accepted or token is None:
            button.setFocus()
            return
        cursor.insertText(token)
        field.setTextCursor(cursor)
        field.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        clamp_window(self)

    def draft_profile(self):
        reactions = {name: Reaction(toggle.isChecked(), tuple(line.strip() for line in editor.toPlainText().splitlines() if line.strip())) for name, (toggle, editor) in self.reactions.items()}
        profile = IntegrationProfile(self.identity.text().strip(), self.label.text().strip(), **reactions,
                                     slot=int(self.slot.value()) if self.module.driver == "afterburner" else None,
                                     power_pct=self.profile.power_pct if self.profile else None,
                                     technical_details=self.technical_details.toPlainText())
        profiles = tuple(item for item in self.module.profiles if self.profile is None or item.id != self.profile.id) + (profile,)
        replace(self.module, profiles=profiles).validate()
        return profile

    def validate_draft(self):
        try:
            draft = self.draft_profile()
            self.save_button.setEnabled(draft != self.profile)
            self.error_label.clear()
        except (ValueError, TypeError):
            self.save_button.setEnabled(False)
            self.error_label.setText(t("invalid_settings"))

    def save(self):
        try:
            self.profile = self.draft_profile()
        except (ValueError, TypeError):
            self.error_label.setText(t("invalid_settings"))
            return
        self.accept()


class IntegrationsEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modules: list[IntegrationDefinition] = []
        self._index = -1
        self._profiles: tuple[IntegrationProfile, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        self.heading = QWidget()
        header = QHBoxLayout(self.heading)
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel(t("integrations"))
        title.setObjectName("page_title")
        header.addWidget(title)
        header.addWidget(QLabel("—"))
        self.selector = QComboBox()
        self.selector.setAccessibleName(t("integrations"))
        self.selector.currentIndexChanged.connect(self.select)
        header.addWidget(self.selector, 1)
        self.management_buttons = {}
        for key, icon, callback in (("integration_add", "add", self.add), ("remove", "delete", self.remove), ("integration_export", "export", self.export_module)):
            button = QToolButton()
            button.setProperty("icon_button", True)
            button.setIcon(QIcon(icon_path(icon)))
            button.setToolTip(t(key))
            button.setAccessibleName(t(key))
            if key == "remove":
                button.setObjectName("danger")
            button.clicked.connect(callback)
            header.addWidget(button)
            self.management_buttons[key] = button
        layout.addWidget(self.heading)
        self.subtitle = hint(t("integration_tabs_hint"))
        layout.addWidget(self.subtitle)
        self.body = ContentTabs()
        self.body.currentChanged.connect(lambda index: self.subtitle.setText(t("rule_range_hint" if index == 3 else "integration_tabs_hint")))
        layout.addWidget(self.body)
        self.empty_hint = hint(t("integration_empty_help"))
        layout.addWidget(self.empty_hint)
        general = QWidget()
        form = QFormLayout(general)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.identity = QLineEdit()
        self.identity.setReadOnly(True)
        self.label = QLineEdit()
        self.enabled = Switch(t("integration_enabled"))
        self.allow_launch = Switch(t("integration_allow_launch"))
        self.path = ExecutableField()
        path_row = QHBoxLayout()
        self.path.setMinimumWidth(0)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(action(t("browse"), self.browse))
        form.addRow(t("integration_name"), self.label)
        form.addRow(t("integration_executable"), path_row)
        path_row.parentWidget().setProperty("wide_field", "long")
        form.addRow(t("integration_enabled"), self.enabled)
        form.addRow(t("integration_allow_launch"), self.allow_launch)
        conditions_group = QWidget()
        conditions_form = QVBoxLayout(conditions_group)
        conditions_form.setContentsMargins(0, 16, 0, 0)
        conditions_form.setSpacing(12)
        conditions_title = QLabel(t("integration_conditions_title"))
        conditions_title.setObjectName("section_title")
        conditions_form.addWidget(conditions_title)
        self.conditions = {}
        for name, key in (("application", "running_applications"), ("input", "rule_input"), ("soc", "battery_level")):
            control = QCheckBox(t(key))
            if name == "soc":
                control.setToolTip(metric_tooltip("battery_level"))
            elif name == "input":
                control.setToolTip(rule_tooltip("rule_input"))
            self.conditions[name] = control
            conditions_form.addWidget(control)
        warning = hint(t("integration_conditions_hint"))
        warning.setObjectName("warning")
        conditions_form.addWidget(warning)
        self.condition_error = QLabel()
        self.condition_error.setObjectName("form_error")
        self.condition_error.setWordWrap(True)
        conditions_form.addWidget(self.condition_error)
        form.addRow(conditions_group)
        technical = CollapsibleSection(t("integration_technical_id"))
        technical.content.addWidget(self.identity)
        form.addRow(technical)
        self.identity.setToolTip(t("integration_id_help"))
        self.body.addTab(general, t("integration_general"))
        mapping_body = QWidget()
        mapping_layout = QVBoxLayout(mapping_body)
        mapping_layout.setContentsMargins(0, 24, 0, 0)
        mapping_layout.setSpacing(24)
        mapping_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.mapping = DataTable((26, 31, 43))
        self.mapping.setProperty("row_limit", 8)
        self.mapping.setAccessibleName(t("integration_mapping"))
        self.mapping.setHorizontalHeaderLabels([t("integration_id"), t("profile_label"), t("integration_arguments")])
        self.mapping.itemChanged.connect(self.mapping_tooltip)
        self.mapping.itemDoubleClicked.connect(lambda item: self.edit_mapping(True))
        mapping_buttons = ActionLayout()
        mapping_buttons.addWidget(action(t("integration_add_command"), lambda: self.edit_mapping(False)))
        mapping_buttons.addWidget(action(t("edit"), lambda: self.edit_mapping(True)))
        mapping_buttons.addStretch()
        remove = action(t("remove"), self.remove_mapping)
        remove.setObjectName("danger")
        mapping_buttons.addWidget(remove)
        mapping_layout.addLayout(mapping_buttons)
        mapping_layout.addWidget(self.mapping)
        mapping_layout.addWidget(hint(t("integration_mapping_hint")))
        self.body.addTab(mapping_body, t("integration_mapping"))
        profiles_body = QWidget()
        profiles_layout = QVBoxLayout(profiles_body)
        profiles_layout.setContentsMargins(0, 24, 0, 0)
        profiles_layout.setSpacing(24)
        profiles_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.profiles = DataTable((26, 48, 26))
        self.profiles.setProperty("row_limit", 8)
        self.profiles.setHorizontalHeaderLabels([t("integration_id"), t("name"), t("profile_number")])
        self.profiles.setAccessibleName(t("integration_profiles"))
        profile_buttons = ActionLayout()
        profile_buttons.addWidget(action(t("integration_add_profile"), lambda: self.edit_profile(False)))
        profile_buttons.addWidget(action(t("edit"), lambda: self.edit_profile(True)))
        profile_buttons.addStretch()
        remove = action(t("remove"), self.remove_profile)
        remove.setObjectName("danger")
        profile_buttons.addWidget(remove)
        profiles_layout.addLayout(profile_buttons)
        profiles_layout.addWidget(self.profiles)
        self.profiles.itemDoubleClicked.connect(lambda item: self.edit_profile(True))
        self.body.addTab(profiles_body, t("integration_profiles"))
        rules_body = QWidget()
        rules_layout = QVBoxLayout(rules_body)
        rules_layout.setContentsMargins(0, 24, 0, 0)
        rules_layout.setSpacing(24)
        rules_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.application_rules = RulesEditor(True)
        self.system_rules = RulesEditor(False)
        self.application_rules_label = QLabel(t("application_rules_title"))
        self.application_rules_label.setObjectName("section_title")
        rules_layout.addWidget(self.application_rules_label)
        rules_layout.addWidget(self.application_rules)
        system_title = QLabel(t("system_rules"))
        system_title.setObjectName("section_title")
        rules_layout.addWidget(system_title)
        rules_layout.addWidget(self.system_rules)
        rules_layout.addWidget(hint(t("rule_priority_hint")))
        for editor in (self.application_rules, self.system_rules):
            editor.changed.connect(self.changed)
        self.body.addTab(rules_body, t("rules_settings"))
        self.enabled.toggled.connect(self.changed)
        for control in self.conditions.values():
            control.toggled.connect(self.update_conditions)

    def update_conditions(self):
        blocked = []
        for name, control in self.conditions.items():
            if control.isChecked():
                continue
            dependent = []
            for editor in (self.application_rules, self.system_rules):
                for rule in editor.rules():
                    used = bool(rule.executable) if name == "application" else rule.input_condition != "any" if name == "input" else rule.min_soc is not None or rule.max_soc is not None
                    if used:
                        dependent.append(rule.id)
            if dependent:
                control.blockSignals(True)
                control.setChecked(True)
                control.blockSignals(False)
                blocked.extend(dependent)
        self.condition_error.setText(t("condition_in_use").format(items=", ".join(dict.fromkeys(blocked))) if blocked else "")
        conditions = tuple(name for name, control in self.conditions.items() if control.isChecked())
        self.application_rules_label.setVisible("application" in conditions)
        for editor in (self.application_rules, self.system_rules):
            editor.configure(self._profiles, conditions)

    def load(self, modules):
        self._modules = list(modules)
        self._index = -1
        self.refresh_selector(0)

    def refresh_selector(self, index):
        self.selector.blockSignals(True)
        self.selector.clear()
        for module in self._modules:
            self.selector.addItem(module.label, module.id)
        self.selector.setCurrentIndex(index if self._modules else -1)
        self.selector.blockSignals(False)
        self._index = -1
        self.select(self.selector.currentIndex())
        self.changed.emit()

    def capture(self, validate_paths=False):
        if self._index < 0:
            return
        old = self._modules[self._index]
        mapping = tuple(CliCommand(self.mapping.item(row, 0).text().strip(), self.mapping.item(row, 1).text().strip(),
                                   template_tokens("{app} " + self.mapping.item(row, 2).text())[1:]) for row in range(self.mapping.rowCount()))
        path = self.path.text().strip()
        if validate_paths and path:
            path = local_executable(path)
        module = replace(old, label=self.label.text().strip(), enabled=self.enabled.isChecked(), executable=path,
                         allow_launch=self.allow_launch.isChecked(),
                         conditions=tuple(name for name, control in self.conditions.items() if control.isChecked()),
                         mapping=mapping, profiles=self._profiles,
                         application_rules=self.application_rules.rules(validate_paths),
                         system_rules=self.system_rules.rules(validate_paths))
        if validate_paths:
            module.validate()
        self._modules[self._index] = module

    def modules(self, validate_paths=False):
        self.capture(validate_paths)
        result = []
        for module in self._modules:
            if validate_paths:
                path = local_executable(module.executable) if module.executable else ""
                rules = tuple(replace(rule, executable=local_executable(rule.executable)) for rule in module.application_rules)
                module = replace(module, executable=path, application_rules=rules)
                module.validate()
            result.append(module)
        return tuple(result)

    def select(self, index):
        try:
            self.capture()
        except (ValueError, AttributeError) as error:
            self.selector.blockSignals(True)
            self.selector.setCurrentIndex(self._index)
            self.selector.blockSignals(False)
            QMessageBox.warning(self, t("error_title"), t("invalid_settings") + "\n" + str(error))
            return
        self._index = index
        self.body.setEnabled(index >= 0)
        self.body.setVisible(index >= 0)
        for key in ("remove", "integration_export"):
            self.management_buttons[key].setEnabled(index >= 0)
        self.empty_hint.setVisible(index < 0)
        if index < 0:
            return
        module = self._modules[index]
        self.identity.setText(module.id)
        self.label.setText(module.label)
        self.enabled.blockSignals(True)
        self.enabled.setChecked(module.enabled)
        self.enabled.blockSignals(False)
        self.path.setText(module.executable)
        self.allow_launch.setChecked(module.allow_launch)
        self.allow_launch.setEnabled(module.driver != "afterburner")
        for name, control in self.conditions.items():
            control.blockSignals(True)
            control.setChecked(name in module.conditions)
            control.blockSignals(False)
        self.mapping.setRowCount(0)
        for command in module.mapping:
            self.add_mapping(command)
        self._profiles = module.profiles
        self.application_rules.configure(self._profiles, module.conditions)
        self.system_rules.configure(self._profiles, module.conditions)
        self.application_rules.set_rules(module.application_rules)
        self.system_rules.set_rules(module.system_rules)
        self.refresh_profiles()

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, t("integration_executable"), "", t("application_files") + " (*.exe *.lnk)")
        if path:
            try:
                self.path.choose(path)
            except (ValueError, OSError):
                QMessageBox.warning(self, t("error_title"), t("invalid_executable"))

    def add_mapping(self, command=None):
        if not isinstance(command, CliCommand):
            command = CliCommand("command_" + uuid.uuid4().hex[:8], t("integration_new_command"), ("--option",))
        row = self.mapping.rowCount()
        self.mapping.insertRow(row)
        for column, value in enumerate((command.id, command.label, subprocess.list2cmdline(command.arguments))):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setForeground(QBrush(QColor(COLORS["input"])))
            elif column == 2:
                item.setForeground(QBrush(QColor(COLORS["muted"])))
            self.mapping.setItem(row, column, item)
        self.mapping.selectRow(row)
        self.mapping.fit_columns()

    def edit_mapping(self, existing):
        if self._index < 0:
            return
        try:
            self.capture()
            module = self._modules[self._index]
            row = self.mapping.currentRow()
            command = module.mapping[row] if existing and row >= 0 else None
            dialog = CommandDialog(module, command, self)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.command is not None:
                if command is not None:
                    self.mapping.item(row, 1).setText(dialog.command.label)
                    self.mapping.item(row, 2).setText(subprocess.list2cmdline(dialog.command.arguments))
                else:
                    self.add_mapping(dialog.command)
                self.changed.emit()
            dialog.deleteLater()
        except (ValueError, AttributeError):
            QMessageBox.warning(self, t("error_title"), t("invalid_settings"))

    def mapping_tooltip(self, item):
        if item.toolTip() != item.text():
            item.setToolTip(item.text())

    def remove_mapping(self):
        row = self.mapping.currentRow()
        if row < 0:
            return
        key = self.mapping.item(row, 0).text().strip()
        if any("{cli:" + key + "}" in command for profile in self._profiles for reaction in (profile.activation, profile.deactivation) for command in reaction.commands):
            QMessageBox.warning(self, t("error_title"), t("integration_in_use"))
            return
        if confirm(self, t("confirm_delete") + "\n" + self.mapping.item(row, 1).text()):
            self.mapping.removeRow(row)
            self.mapping.fit_columns()
            self.changed.emit()

    def refresh_profiles(self):
        row = self.profiles.currentRow()
        self.profiles.setRowCount(len(self._profiles))
        for index, profile in enumerate(self._profiles):
            for column, value in enumerate((profile.id, profile.label, str(profile.slot) if profile.slot else "—")):
                item = QTableWidgetItem(value)
                item.setToolTip(profile_tooltip(profile))
                if column == 0:
                    item.setForeground(QBrush(QColor(COLORS["input"])))
                self.profiles.setItem(index, column, item)
        self.profiles.selectRow(max(0, min(row, len(self._profiles) - 1)))
        self.profiles.fit_columns()
        self.update_conditions()

    def edit_profile(self, existing):
        if self._index < 0:
            return
        try:
            self.capture()
            module = self._modules[self._index]
            row = self.profiles.currentRow()
            profile = self._profiles[row] if existing and row >= 0 else None
            dialog = ProfileDialog(module, profile, self)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.profile is not None:
                profiles = list(self._profiles)
                if profile:
                    profiles[row] = dialog.profile
                else:
                    profiles.append(dialog.profile)
                self._profiles = tuple(profiles)
                self.refresh_profiles()
                self.refresh_rule_profiles()
                self.changed.emit()
            dialog.deleteLater()
        except (ValueError, AttributeError) as error:
            QMessageBox.warning(self, t("error_title"), t("invalid_settings") + "\n" + str(error))

    def refresh_rule_profiles(self):
        for editor in (self.application_rules, self.system_rules):
            rules = editor.rules()
            editor.configure(self._profiles, tuple(name for name, control in self.conditions.items() if control.isChecked()))
            editor.set_rules(rules)

    def remove_profile(self):
        row = self.profiles.currentRow()
        if row < 0:
            return
        profile = self._profiles[row]
        if any(rule.profile_id == profile.id for editor in (self.application_rules, self.system_rules) for rule in editor.rules()):
            QMessageBox.warning(self, t("error_title"), t("integration_in_use"))
            return
        if not confirm(self, t("confirm_delete") + "\n" + profile.label):
            return
        self._profiles = tuple(item for item in self._profiles if item.id != profile.id)
        self.refresh_profiles()
        self.refresh_rule_profiles()
        self.changed.emit()

    def add_module(self, module):
        self.capture()
        if any(item.id == module.id for item in self._modules):
            raise ValueError(t("integration_duplicate"))
        self._modules.append(module)
        self.refresh_selector(len(self._modules) - 1)

    def add(self):
        try:
            self.capture()
            dialog = IntegrationCreationDialog(tuple(self._modules), self)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.module is not None:
                self.add_module(dialog.module)
            dialog.deleteLater()
        except (ValueError, AttributeError) as error:
            QMessageBox.warning(self, t("error_title"), t("invalid_settings") + "\n" + str(error))

    def remove(self):
        if self._index >= 0:
            if not confirm(self, t("confirm_delete") + "\n" + self._modules[self._index].label):
                return
            self._modules.pop(self._index)
            self.refresh_selector(max(0, self._index - 1))

    def export_module(self):
        if self._index < 0:
            return
        try:
            self.capture()
            module = self._modules[self._index]
            content = json.dumps(module.to_dict(), ensure_ascii=False, indent=2) + "\n"
            path, _ = QFileDialog.getSaveFileName(self, t("integration_export"), module.id + ".json", "JSON (*.json)")
            if path:
                Path(path).write_text(content, encoding="utf-8")
        except (OSError, ValueError, TypeError, AttributeError) as error:
            QMessageBox.warning(self, t("error_title"), t("invalid_settings") + "\n" + str(error))

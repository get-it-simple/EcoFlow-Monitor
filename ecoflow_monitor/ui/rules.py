from dataclasses import replace
import ntpath
import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QTableWidgetItem, QVBoxLayout, QWidget

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationProfile, IntegrationRule
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.dialogs import confirm
from ecoflow_monitor.ui.common import action, hint, profile_tooltip
from ecoflow_monitor.ui.percent_editor import PercentEditor
from ecoflow_monitor.ui.components import ActionLayout, DataTable, DesignDialog, FormLayout, SelectField
from ecoflow_monitor.ui.metrics import rule_tooltip


def optional_percent(default=0, compact=False) -> PercentEditor:
    return PercentEditor(optional=True, default=default, compact=compact)


class RuleDialog(DesignDialog):
    def __init__(self, profiles, conditions, application, rule, parent=None, creating=False):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle(t("rule_editor"))
        self.resize(760, 680)
        self.original = rule
        self.rule = rule
        self.creating = creating
        self.profiles = profiles
        self.conditions = conditions
        self.application = application
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(24)
        form = FormLayout()
        self.enabled = QCheckBox(t("rule_enabled"))
        self.enabled.setChecked(rule.enabled)
        layout.addWidget(self.enabled)
        self.path = QLineEdit(rule.executable, self)
        if application:
            path = QHBoxLayout()
            path.addWidget(self.path, 1)
            path.addWidget(action(t("browse"), self.browse))
            form.addRow(t("rule_application"), path)
        else:
            self.path.hide()
        self.input = SelectField(self)
        for value in ("any", "present", "absent"):
            self.input.addItem(t("rule_" + value), value)
        self.input.setCurrentIndex(self.input.findData(rule.input_condition))
        self.input.setToolTip(rule_tooltip("rule_input"))
        if "input" in conditions:
            form.addRow(t("rule_input"), self.input)
        else:
            self.input.hide()
        self.minimum = optional_percent(0)
        self.maximum = optional_percent(100)
        self.minimum.setParent(self)
        self.maximum.setParent(self)
        for key, field, value in (("rule_min", self.minimum, rule.min_soc), ("rule_max", self.maximum, rule.max_soc)):
            field.setValue(-1 if value is None else value)
            field.setAccessibleName(t(key))
            field.setToolTip(rule_tooltip(key))
            if "soc" in conditions:
                form.addRow(t(key), field)
            else:
                field.hide()
        self.profile = SelectField()
        for profile in profiles:
            self.profile.addItem(profile.label, profile.id)
            self.profile.setItemData(self.profile.count() - 1, profile_tooltip(profile), Qt.ItemDataRole.ToolTipRole)
        self.profile.setCurrentIndex(self.profile.findData(rule.profile_id))
        self.profile.currentIndexChanged.connect(self.update_profile_tooltip)
        self.update_profile_tooltip()
        form.addRow(t("rule_profile"), self.profile)
        layout.addLayout(form)
        layout.addWidget(hint(t("rule_range_hint")))
        self.error = QLabel()
        self.error.setObjectName("form_error")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        layout.addStretch()
        buttons = ActionLayout()
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        self.save_button = action(t("save"), self.save)
        self.save_button.setObjectName("primary")
        buttons.addWidget(self.save_button)
        self.set_footer(buttons)
        self.enabled.toggled.connect(self.validate_draft)
        self.path.textChanged.connect(self.validate_draft)
        self.input.currentIndexChanged.connect(self.validate_draft)
        self.profile.currentIndexChanged.connect(self.validate_draft)
        self.minimum.valueChanged.connect(self.validate_draft)
        self.maximum.valueChanged.connect(self.validate_draft)
        self.validate_draft()

    def update_profile_tooltip(self):
        self.profile.setToolTip(self.profile.currentData(Qt.ItemDataRole.ToolTipRole) or "")

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, t("add_application"), "", t("application_files") + " (*.exe *.lnk)")
        if path:
            try:
                self.path.setText(local_executable(path))
            except (ValueError, OSError):
                self.error.setText(t("invalid_executable"))

    def draft(self, validate_path=False):
        path = self.path.text().strip() if self.application else ""
        if self.application and not path:
            raise ValueError(t("invalid_executable"))
        if path and validate_path:
            path = local_executable(path)
        rule = replace(self.original, executable=path, enabled=self.enabled.isChecked(),
                       input_condition=self.input.currentData() if "input" in self.conditions else "any",
                       min_soc=self.minimum.value() if "soc" in self.conditions and self.minimum.value() >= 0 else None,
                       max_soc=self.maximum.value() if "soc" in self.conditions and self.maximum.value() >= 0 else None,
                       profile_id=self.profile.currentData() or "")
        rule.validate()
        return rule

    def validate_draft(self):
        try:
            rule = self.draft()
            self.error.clear()
            self.save_button.setEnabled(self.creating or rule != self.original)
        except ValueError:
            self.error.setText(t("invalid_rule"))
            self.save_button.setEnabled(False)

    def save(self):
        try:
            self.rule = self.draft(True)
        except (ValueError, OSError):
            self.error.setText(t("invalid_rule"))
            return
        self.accept()


class RuleTable(DataTable):
    def __init__(self, editor):
        super().__init__((1, 3, 2, 2, 3), editor)
        self.editor = editor
        self.setProperty("row_limit", 5)
        self.setAcceptDrops(editor.application)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if self.editor.application and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self.editor.application and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self.editor.application and event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                self.editor.add_application(url.toLocalFile())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class RulesEditor(QWidget):
    changed = Signal()

    def __init__(self, application: bool, parent=None):
        super().__init__(parent)
        self.application = application
        self.profiles: tuple[IntegrationProfile, ...] = ()
        self.conditions = ("application", "input", "soc")
        self._rules: list[IntegrationRule] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.table = RuleTable(self)
        self.table.setHorizontalHeaderLabels([t("rule_enabled"), t("rule_application") if application else t("rule_editor"), t("rule_input"), t("rule_range"), t("rule_profile")])
        self.table.itemDoubleClicked.connect(lambda item: self.edit_rule())
        layout.addWidget(self.table)
        self.empty_hint = hint(t("rule_empty"))
        layout.addWidget(self.empty_hint)
        buttons = ActionLayout()
        self.add_buttons = []
        if application:
            button = action(t("add_application"), self.browse)
            self.add_buttons.append(button)
            buttons.addWidget(button)
        button = action(t("add_rule"), self.add_rule)
        self.add_buttons.append(button)
        buttons.addWidget(button)
        buttons.addWidget(action(t("edit"), self.edit_rule))
        remove = action(t("remove"), self.remove)
        remove.setObjectName("danger")
        buttons.addWidget(remove)
        buttons.addStretch()
        buttons.addWidget(action(t("move_up_action"), lambda: self.move_rule(-1)))
        buttons.addWidget(action(t("move_down_action"), lambda: self.move_rule(1)))
        layout.addLayout(buttons)

    def configure(self, profiles, conditions) -> None:
        self.profiles = profiles
        self.conditions = conditions
        self.table.setColumnHidden(2, "input" not in conditions)
        self.table.setColumnHidden(3, "soc" not in conditions)
        self.setVisible(not self.application or "application" in conditions)
        for button in self.add_buttons:
            button.setEnabled(bool(profiles))
            button.setToolTip("" if profiles else t("rule_needs_profile"))
        self.refresh()

    def refresh(self):
        selected = self.table.currentRow()
        self.table.setRowCount(0)
        profiles = {profile.id: profile for profile in self.profiles}
        for row, rule in enumerate(self._rules):
            self.table.insertRow(row)
            enabled = QCheckBox()
            enabled.setAccessibleName(t("rule_enabled"))
            enabled.setChecked(rule.enabled)
            enabled.toggled.connect(lambda value, key=rule.id: self.set_enabled(key, value))
            self.table.setCellWidget(row, 0, enabled)
            profile = profiles.get(rule.profile_id)
            lower = "—" if rule.min_soc is None else f"{rule.min_soc:g}"
            upper = "—" if rule.max_soc is None else f"{rule.max_soc:g}"
            values = (ntpath.basename(rule.executable) if self.application else t("rule_number").format(number=row + 1),
                      t("rule_" + rule.input_condition), lower + "–" + upper + " %", profile.label if profile else rule.profile_id)
            for column, text in enumerate(values, 1):
                item = QTableWidgetItem(text)
                item.setToolTip(rule.executable if column == 1 and self.application else profile_tooltip(profile) if column == 4 and profile else text)
                self.table.setItem(row, column, item)
        self.table.selectRow(max(0, min(selected, len(self._rules) - 1)))
        self.empty_hint.setVisible(not self._rules)
        self.table.fit_columns()

    def set_enabled(self, key, enabled):
        self._rules = [replace(rule, enabled=enabled) if rule.id == key else rule for rule in self._rules]
        self.changed.emit()

    def set_rules(self, rules: tuple[IntegrationRule, ...]) -> None:
        self._rules = list(rules)
        self.refresh()

    def append(self, rule: IntegrationRule) -> None:
        self._rules.append(rule)
        self.refresh()
        self.table.selectRow(len(self._rules) - 1)
        self.changed.emit()

    def rules(self, validate_paths: bool = False) -> tuple[IntegrationRule, ...]:
        rules = tuple(replace(rule, executable=local_executable(rule.executable)) if validate_paths and self.application else rule for rule in self._rules)
        if validate_paths:
            for rule in rules:
                rule.validate()
        return rules

    def edit_rule(self, new_rule=None):
        if not self.profiles:
            return
        row = self.table.currentRow()
        creating = isinstance(new_rule, IntegrationRule)
        if not creating and row < 0:
            return
        original = new_rule if creating else self._rules[row]
        dialog = RuleDialog(self.profiles, self.conditions, self.application, original, self, creating=creating)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if creating:
                self.append(dialog.rule)
            else:
                self._rules[row] = dialog.rule
                self.refresh()
                self.changed.emit()
        dialog.deleteLater()

    def add_application(self, path: str) -> None:
        if not self.profiles:
            return
        try:
            path = local_executable(path)
            for row, rule in enumerate(self._rules):
                if normalized_executable(rule.executable) == normalized_executable(path):
                    self.table.selectRow(row)
                    return
            self.edit_rule(IntegrationRule(uuid.uuid4().hex, self.profiles[0].id, executable=path))
        except (OSError, ValueError):
            QMessageBox.warning(self, t("error_title"), t("invalid_executable"))

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("add_application"), "", t("application_files") + " (*.exe *.lnk)")
        if path:
            self.add_application(path)

    def add_rule(self) -> None:
        if not self.profiles:
            return
        path = ""
        if self.application:
            row = self.table.currentRow()
            if row < 0:
                self.browse()
                return
            path = self._rules[row].executable
        self.edit_rule(IntegrationRule(uuid.uuid4().hex, self.profiles[0].id, executable=path))

    def remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0 and confirm(self, t("confirm_delete")):
            self._rules.pop(row)
            self.refresh()
            self.changed.emit()

    def move_rule(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if 0 <= row < len(self._rules) and 0 <= target < len(self._rules):
            self._rules[row], self._rules[target] = self._rules[target], self._rules[row]
            self.refresh()
            self.table.selectRow(target)
            self.changed.emit()

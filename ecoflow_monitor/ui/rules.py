import uuid
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QFileDialog, QHeaderView, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationProfile, IntegrationRule
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.dialogs import confirm
from ecoflow_monitor.ui.common import action, profile_tooltip
from ecoflow_monitor.ui.percent_editor import PercentEditor
from ecoflow_monitor.ui.components import ActionLayout, SelectField as QComboBox
from ecoflow_monitor.ui.metrics import rule_tooltip


def optional_percent(default=0, compact=False) -> PercentEditor:
    return PercentEditor(optional=True, default=default, compact=compact)


class RuleTable(QTableWidget):
    def __init__(self, editor):
        super().__init__(0, 6 if editor.application else 5)
        self.editor = editor
        self.setAcceptDrops(editor.application)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setShowGrid(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(48)
        self.horizontalHeader().setHighlightSections(False)
        self.verticalHeader().setHighlightSections(False)

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
    def __init__(self, application: bool, parent=None):
        super().__init__(parent)
        self.application = application
        self.profiles: tuple[IntegrationProfile, ...] = ()
        self.conditions = ("application", "input", "soc")
        layout = QVBoxLayout(self)
        self.table = RuleTable(self)
        headings = [t("rule_enabled")]
        if application:
            headings.append(t("rule_application"))
        headings.extend(t(key) for key in ("rule_input", "rule_min", "rule_max", "rule_profile"))
        self.table.setHorizontalHeaderLabels(headings)
        column = 2 if application else 1
        for offset, key in enumerate(("rule_input", "rule_min", "rule_max")):
            heading = QTableWidgetItem(t(key))
            heading.setToolTip(rule_tooltip(key))
            self.table.setHorizontalHeaderItem(column + offset, heading)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        if application:
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        else:
            for index in range(1, self.table.columnCount()):
                self.table.horizontalHeader().setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table)
        buttons = ActionLayout()
        if application:
            buttons.addWidget(action(t("add_application"), self.browse))
        buttons.addWidget(action(t("add_rule"), self.add_rule))
        buttons.addWidget(action(t("remove"), self.remove))
        up = action("↑", lambda: self.move_rule(-1))
        down = action("↓", lambda: self.move_rule(1))
        up.setToolTip(t("move_up"))
        down.setToolTip(t("move_down"))
        buttons.addWidget(up)
        buttons.addWidget(down)
        layout.addLayout(buttons)

    def configure(self, profiles, conditions) -> None:
        self.profiles = profiles
        self.conditions = conditions
        column = 2 if self.application else 1
        self.table.setColumnHidden(column, "input" not in conditions)
        self.table.setColumnHidden(column + 1, "soc" not in conditions)
        self.table.setColumnHidden(column + 2, "soc" not in conditions)
        self.setVisible(not self.application or "application" in conditions)

    def set_rules(self, rules: tuple[IntegrationRule, ...]) -> None:
        self.table.setRowCount(0)
        for rule in rules:
            self.append(rule)

    def append(self, rule: IntegrationRule) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        enabled = QCheckBox()
        enabled.setChecked(rule.enabled)
        enabled.setProperty("rule_id", rule.id)
        self.table.setCellWidget(row, 0, enabled)
        column = 1
        if self.application:
            item = QTableWidgetItem(rule.executable)
            item.setToolTip(rule.executable)
            self.table.setItem(row, column, item)
            column += 1
        condition = QComboBox()
        for value in ("any", "present", "absent"):
            condition.addItem(t("rule_" + value), value)
        condition.setCurrentIndex(max(0, condition.findData(rule.input_condition)))
        condition.setToolTip(rule_tooltip("rule_input"))
        self.table.setCellWidget(row, column, condition)
        for offset, value in ((1, rule.min_soc), (2, rule.max_soc)):
            spin = optional_percent(0 if offset == 1 else 100, compact=True)
            spin.setAccessibleName(t("rule_min" if offset == 1 else "rule_max"))
            spin.setToolTip(rule_tooltip("rule_min" if offset == 1 else "rule_max"))
            spin.setValue(-1 if value is None else value)
            self.table.setCellWidget(row, column + offset, spin)
        profile = QComboBox()
        for item in self.profiles:
            profile.addItem(item.label, item.id)
            profile.setItemData(profile.count() - 1, profile_tooltip(item), Qt.ItemDataRole.ToolTipRole)
        profile.currentIndexChanged.connect(lambda: profile.setToolTip(profile.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
        profile.setCurrentIndex(profile.findData(rule.profile_id))
        profile.setToolTip(profile.currentData(Qt.ItemDataRole.ToolTipRole) or "")
        self.table.setCellWidget(row, column + 3, profile)
        self.table.selectRow(row)
        self.table.resizeRowsToContents()

    def rules(self, validate_paths: bool = False) -> tuple[IntegrationRule, ...]:
        result = []
        for row in range(self.table.rowCount()):
            enabled = cast(QCheckBox, self.table.cellWidget(row, 0))
            column = 2 if self.application else 1
            path = self.table.item(row, 1).text().strip() if self.application else ""
            if path and validate_paths:
                path = local_executable(path)
            minimum = cast(PercentEditor, self.table.cellWidget(row, column + 1)).value()
            maximum = cast(PercentEditor, self.table.cellWidget(row, column + 2)).value()
            rule = IntegrationRule(
                enabled.property("rule_id"), cast(QComboBox, self.table.cellWidget(row, column + 3)).currentData(),
                enabled.isChecked(), path, cast(QComboBox, self.table.cellWidget(row, column)).currentData(),
                minimum if minimum >= 0 else None, maximum if maximum >= 0 else None,
            )
            if validate_paths:
                rule.validate()
            result.append(rule)
        return tuple(result)

    def add_application(self, path: str) -> None:
        try:
            path = local_executable(path)
            for row, rule in enumerate(self.rules()):
                if normalized_executable(rule.executable) == normalized_executable(path):
                    self.table.selectRow(row)
                    return
            self.append(IntegrationRule(uuid.uuid4().hex, self.profiles[0].id if self.profiles else "missing", executable=path))
        except (OSError, ValueError):
            QMessageBox.warning(self, t("error_title"), t("invalid_executable"))

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("add_application"), "", t("application_files") + " (*.exe *.lnk)")
        if path:
            self.add_application(path)

    def add_rule(self) -> None:
        path = ""
        if self.application:
            row = self.table.currentRow()
            if row < 0:
                self.browse()
                return
            path = self.table.item(row, 1).text()
        self.append(IntegrationRule(uuid.uuid4().hex, self.profiles[0].id if self.profiles else "missing", executable=path))

    def remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0 and confirm(self, t("confirm_delete")):
            self.table.removeRow(row)

    def move_rule(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if 0 <= row < self.table.rowCount() and 0 <= target < self.table.rowCount():
            rules = list(self.rules())
            rules[row], rules[target] = rules[target], rules[row]
            self.set_rules(tuple(rules))
            self.table.selectRow(target)

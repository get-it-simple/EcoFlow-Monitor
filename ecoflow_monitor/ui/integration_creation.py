from dataclasses import replace
import json
from pathlib import Path
import uuid

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.integrations import IntegrationDefinition, afterburner_example
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, hint
from ecoflow_monitor.ui.components import ActionLayout, DesignDialog, FormLayout
from ecoflow_monitor.ui.dialogs import confirm


class IntegrationCreationDialog(DesignDialog):
    def __init__(self, modules, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle(t("integration_add"))
        self.resize(760, 520)
        self.modules = modules
        self.module = None
        self.template = IntegrationDefinition(uuid.uuid4().hex, "")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(24)
        form = FormLayout()
        self.label = QLineEdit()
        self.path = QLineEdit()
        path = QHBoxLayout()
        path.addWidget(self.path, 1)
        path.addWidget(action(t("browse"), self.browse))
        form.addRow(t("integration_name"), self.label)
        form.addRow(t("integration_executable"), path)
        layout.addLayout(form)
        options = ActionLayout()
        options.addWidget(action(t("integration_import"), self.import_module))
        options.addWidget(action(t("integration_use_example"), self.use_example))
        options.addStretch()
        layout.addLayout(options)
        layout.addWidget(hint(t("integration_creation_hint")))
        self.error = QLabel()
        self.error.setObjectName("form_error")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        layout.addStretch()
        buttons = ActionLayout()
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        self.save_button = action(t("integration_add"), self.save)
        self.save_button.setObjectName("primary")
        buttons.addWidget(self.save_button)
        self.set_footer(buttons)
        self.label.textChanged.connect(self.validate_draft)
        self.path.textChanged.connect(self.validate_draft)
        self.validate_draft()

    def draft(self, validate_path=False):
        path = self.path.text().strip()
        if path and validate_path:
            path = local_executable(path)
        module = replace(self.template, label=self.label.text().strip(), executable=path, enabled=False, auto_enabled=False)
        if any(existing.id == module.id for existing in self.modules):
            raise ValueError(t("integration_duplicate"))
        module.validate()
        return module

    def validate_draft(self):
        try:
            self.draft()
            self.error.clear()
            self.save_button.setEnabled(True)
        except ValueError as error:
            self.error.setText(str(error) if self.label.text().strip() else "")
            self.save_button.setEnabled(False)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, t("integration_executable"), "", t("application_files") + " (*.exe *.lnk)")
        if path:
            try:
                self.path.setText(local_executable(path))
            except (ValueError, OSError):
                self.error.setText(t("invalid_executable"))

    def fill(self, module):
        if any(existing.id == module.id for existing in self.modules):
            self.error.setText(t("integration_duplicate"))
            return
        self.template = replace(module, enabled=False, auto_enabled=False)
        self.label.setText(module.label)
        self.path.setText(module.executable)
        self.validate_draft()

    def import_module(self):
        path, _ = QFileDialog.getOpenFileName(self, t("integration_import"), "", "JSON (*.json)")
        if not path:
            return
        try:
            module = IntegrationDefinition.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
            message = t("import_preview").format(name=module.label, profiles=len(module.profiles), rules=len(module.application_rules) + len(module.system_rules))
            if confirm(self, message, "integration_import"):
                self.fill(module)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.error.setText(t("invalid_integration"))

    def use_example(self):
        if (self.label.text().strip() or self.path.text()) and not confirm(self, t("replace_integration_draft"), "integration_use_example"):
            return
        self.fill(afterburner_example())

    def save(self):
        try:
            self.module = self.draft(True)
        except (ValueError, OSError):
            self.error.setText(t("invalid_integration"))
            return
        self.accept()

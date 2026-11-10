from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QVBoxLayout

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.common import action, hint
from ecoflow_monitor.ui.components import DesignDialog
from ecoflow_monitor.ui.sections import CollapsibleSection


class LanguageGuideDialog(DesignDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle(t("language_guide"))
        self.resize(620, 340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(24)
        layout.addWidget(hint(t("language_guide_summary")))
        details = CollapsibleSection(t("language_guide_details"))
        details.content.addWidget(hint(t("language_guide_text")))
        layout.addWidget(details)
        layout.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        understood = action(t("understood"), self.accept)
        understood.setObjectName("primary")
        row.addWidget(understood)
        layout.addLayout(row)


def confirm(parent, message, action_key="remove"):
    prompt = QMessageBox(parent)
    prompt.setWindowTitle(t("confirm_delete") if action_key == "remove" else t("integration_import"))
    prompt.setText(message)
    prompt.setStyleSheet(settings_style())
    accept = prompt.addButton(t(action_key), QMessageBox.ButtonRole.DestructiveRole if action_key == "remove" else QMessageBox.ButtonRole.AcceptRole)
    cancel = prompt.addButton(t("cancel"), QMessageBox.ButtonRole.RejectRole)
    prompt.setDefaultButton(cancel)
    prompt.exec()
    accepted = prompt.clickedButton() is accept
    prompt.deleteLater()
    return accepted

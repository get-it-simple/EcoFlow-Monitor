from PySide6.QtWidgets import QMessageBox

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.theme import settings_style


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

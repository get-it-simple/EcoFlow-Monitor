from PySide6.QtWidgets import QFrame, QVBoxLayout

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, hint


class CredentialRenewalBanner(QFrame):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        self.message = hint("")
        self.message.setObjectName("warning")
        layout.addWidget(self.message)
        self.renew_button = action(t("onepassword_renew"), controller.renew_credentials)
        layout.addWidget(self.renew_button)
        controller.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        controller = self.controller
        self.setVisible(controller.settings.credential_source == "1password" and controller.credential_renewal_required)
        notice = controller.notice if controller.notice.startswith("onepassword_") else "onepassword_cache_expired"
        self.message.setText(t("onepassword_waiting") if controller.credential_renewal_pending else t(notice))
        self.renew_button.setText(t("onepassword_renew"))
        self.renew_button.setEnabled("api" not in controller._active and not controller.credential_renewal_pending)

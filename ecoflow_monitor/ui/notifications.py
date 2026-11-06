from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QToolButton

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.reference_style import icon_path


def profile_status(module, state):
    requested = next((profile.label for profile in module.profiles if profile.id == state.requested_profile), "")
    status = t("profile_requested").format(number=requested) if requested else t("integration_no_request")
    if state.busy:
        status += " · " + t("integration_busy")
    elif state.result and state.result.success:
        status += " · " + t("integration_command_succeeded" if state.result.completed else "integration_no_commands")
    if state.result and not state.result.success:
        result = state.result
        status += "\n" + t("integration_failure").format(phase=t(result.phase) if result.phase else "", step=result.step, completed=result.completed, error=t(result.error))
        if result.exit_code is not None:
            status += " · " + t("integration_exit_code").format(code=result.exit_code)
        if result.winerror is not None:
            status += " · " + t("integration_windows_error").format(code=result.winerror)
    return status


class NotificationIndicator(QToolButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("notifications")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.has_warning = False

    def refresh(self, controller, hotkey_notice=""):
        notices = list(dict.fromkeys(t(value) for value in (controller.notice, hotkey_notice) if value))
        warning = bool(hotkey_notice or controller.notice and controller.notice != "integration_helper_ready")
        for module in controller.settings.integrations:
            if not module.enabled:
                continue
            state = controller.integration_states[module.id]
            observation = state.observation
            notices.append(module.label + "\n" + t(observation.detail or observation.availability) + "\n" + profile_status(module, state))
            warning = warning or not observation.ready or bool(state.result and not state.result.success)
        self.has_warning = warning
        self.setIcon(QIcon(icon_path("notification_warning" if warning else "notification")))
        tooltip = "\n\n".join(notices)
        self.setToolTip(tooltip)
        self.setAccessibleName(t("notifications"))
        self.setAccessibleDescription(tooltip)
        self.setVisible(bool(tooltip))

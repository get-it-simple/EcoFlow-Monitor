from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout

from ecoflow_monitor.i18n import get_current_language, t
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.components import DesignDialog
from ecoflow_monitor.ui.common import effective_scale, scaled_font
from ecoflow_monitor.ui.resize import WindowResize
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.tokens import COLORS, RADIUS


@dataclass(frozen=True)
class Notice:
    identity: str
    title: str
    message: str
    category: str
    warning: bool = False
    target: str = ""
    module_id: str = ""


def current_notices(controller, hotkey_notice=""):
    notices = []
    if getattr(controller, "startup_pending", False):
        notices.append(Notice("startup", t("startup_consent_title"), t("startup_action_required"), t("connection_settings"), False, "startup"))
    if controller.notice:
        target = "connection_settings"
        if controller.notice.startswith(("integration_", "profile_")):
            target = "integrations"
        elif controller.notice.startswith(("onepassword_", "credential", "authentication")):
            target = "connection_service"
        elif controller.notice.startswith(("logging_", "device_diagnostics_")):
            target = "connection_diagnostics"
        category = "connection_settings" if target.startswith("connection_") else target
        notices.append(Notice("general", t("notifications"), t(controller.notice), t(category), controller.notice != "integration_helper_ready", target))
    if hotkey_notice:
        notices.append(Notice("hotkeys", t("hotkeys_settings"), t(hotkey_notice), t("hotkeys_settings"), True, "hotkeys_settings"))
    for module in controller.settings.integrations:
        if not module.enabled:
            continue
        state = controller.integration_states[module.id]
        observation = state.observation
        warning = not observation.ready or bool(state.result and not state.result.success)
        notices.append(Notice("integration:" + module.id, module.label,
                              t(observation.detail or observation.availability) + "\n" + profile_status(module, state),
                              t("integrations"), warning, "integrations", module.id))
    return tuple(sorted(notices, key=lambda notice: not notice.warning))


class NotificationWindow(DesignDialog):
    def __init__(self, indicator):
        super().__init__(indicator.window())
        self.indicator = indicator
        self.setObjectName("settings_dialog")
        self.setWindowTitle(t("notification_center"))
        self.resize(620, 480)
        self.setModal(False)
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(24, 24, 24, 24)
        self.content.setSpacing(16)
        self.entries = None
        self._presentation = None
        self.buttons = {}
        self.resize_controls = WindowResize(self)
        self.present(indicator.entries)

    def present(self, entries):
        self.setWindowTitle(t("notification_center"))
        if hasattr(self, "titlebar"):
            self.titlebar.title.setText(self.windowTitle())
        scale = effective_scale(self, self.indicator.controller.ui_settings.scale)
        scaled_font(self, scale)
        self.resize_controls.scale = scale
        self.setMinimumSize(QSize(round(320 * scale), round(240 * scale)).boundedTo(self.screen().availableGeometry().size()))
        if self._framed:
            self.setStyleSheet(settings_style(scale=scale) + f"QDialog#settings_dialog {{ background: transparent; }} QFrame#dialog_shell {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: {RADIUS['window']}px; }}")
        self.content.setContentsMargins(*(round(24 * scale),) * 4)
        self.content.setSpacing(round(16 * scale))
        self.resize_controls.layout()
        presentation = (entries, get_current_language(), scale)
        if presentation == self._presentation:
            return
        self._presentation = presentation
        focused = next((key for key, button in self.buttons.items() if button.hasFocus()), None)
        self.entries = entries
        self.buttons = {}
        while self.content.count():
            widget = self.content.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        if not entries:
            empty = QLabel(t("notifications_empty"))
            empty.setWordWrap(True)
            self.content.addWidget(empty)
            empty.show()
        for notice in entries:
            card = QFrame(self)
            card.setObjectName("card")
            row = QHBoxLayout(card)
            row.setContentsMargins(*(round(16 * scale),) * 4)
            text = QVBoxLayout()
            for value, role in ((notice.title, "section_title"), (notice.category, "secondary"), (notice.message, "")):
                label = QLabel(value, card)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setObjectName(role)
                text.addWidget(label)
            row.addLayout(text, 1)
            if notice.target:
                button = QToolButton(card)
                button.setProperty("icon_button", True)
                button.setIcon(QIcon(icon_path("settings")))
                button.setAccessibleName(t("notification_open").format(section=notice.title))
                button.setToolTip(button.accessibleName())
                button.clicked.connect(lambda checked=False, item=notice: self.navigate(item))
                row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
                self.buttons[notice.identity] = button
            self.content.addWidget(card)
        self.content.addStretch()
        if focused in self.buttons:
            self.buttons[focused].setFocus()

    def navigate(self, notice):
        self.close()
        self.indicator.navigate_requested.emit(notice.target, notice.module_id)

    def showEvent(self, event):
        super().showEvent(event)
        self.resize_controls.layout()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.indicator.setFocus()


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
    navigate_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("notifications")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.has_warning = False
        self.entries = ()
        self.dialog = None
        self.clicked.connect(self.open_notifications)

    def open_notifications(self):
        if self.dialog is None:
            self.dialog = NotificationWindow(self)
        self.dialog.present(self.entries)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def refresh(self, controller, hotkey_notice=""):
        self.controller = controller
        self.entries = current_notices(controller, hotkey_notice)
        warning = any(notice.warning for notice in self.entries)
        self.has_warning = warning
        self.setIcon(QIcon(icon_path("notification_warning" if warning else "notification")))
        tooltip = "\n\n".join(notice.title + "\n" + notice.message for notice in self.entries) or t("notifications_empty")
        self.setToolTip(tooltip)
        self.setAccessibleName(t("notifications"))
        self.setAccessibleDescription(tooltip)
        self.setVisible(True)
        if self.dialog is not None:
            self.dialog.present(self.entries)

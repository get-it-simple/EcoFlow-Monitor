from dataclasses import replace

from PySide6.QtCore import QObject, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QToolButton

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.tokens import RADIUS


def configure_overlay_button(button, scale):
    size = round(36 * scale)
    border = max(1, round(scale))
    content = size - 2 * border
    button.setStyleSheet(f"QToolButton {{ padding: 0; border-width: {border}px; min-height: {content}px; max-height: {content}px; min-width: {content}px; max-width: {content}px; border-radius: {round(RADIUS['control'] * scale)}px; }} QToolButton::menu-indicator {{ image: none; }}")
    button.setFixedSize(size, size)
    button.setIconSize(QSize(round(20 * scale), round(20 * scale)))


class OverlayControls(QObject):
    def __init__(self, window, controller, kind):
        super().__init__(window)
        self.window = window
        self.controller = controller
        self.kind = kind
        self.recovery_ready = False
        self.request = None
        self.pin = QToolButton(window)
        self.pin.setIcon(QIcon(icon_path("pin")))
        self.pin.setCheckable(True)
        self.pin.clicked.connect(lambda checked: self.change(pinned=checked))
        self.menu = QMenu(window)
        self.menu.aboutToShow.connect(self.refresh_menu)
        self.menu_button = QToolButton(window)
        self.menu_button.setIcon(QIcon(icon_path("more")))
        self.menu_button.setMenu(self.menu)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

    @property
    def preferences(self):
        if self.kind == "widget":
            return self.controller.settings.widget.overlay
        return getattr(self.controller.settings, self.kind + "_overlay")

    def change(self, **changes):
        preferences = replace(self.preferences, **changes)
        if self.request is not None:
            return self.request(self.kind, preferences)
        if preferences.click_through:
            return False
        if self.kind == "widget":
            self.controller.update_widget(overlay=preferences, overlay_fields=tuple(changes))
        else:
            self.controller.update_preferences(**{self.kind + "_overlay": preferences})
        return True

    def refresh_menu(self):
        self.menu.clear()
        for key, label in (("position_locked", "position_lock"), ("click_through", "click_through")):
            action = self.menu.addAction(t(label))
            action.setCheckable(True)
            action.setChecked(getattr(self.preferences, key))
            action.triggered.connect(lambda checked, name=key: self.change(**{name: checked}))

    def apply(self):
        preferences = self.controller.ui_settings.widget.overlay if self.kind == "widget" else self.preferences
        self.pin.setChecked(preferences.pinned)
        self.pin.setToolTip(t("pin_widget"))
        self.pin.setAccessibleName(t("pin_widget"))
        self.menu_button.setToolTip(t("widget_controls"))
        self.menu_button.setAccessibleName(t("widget_controls"))
        self.window.setProperty("position_locked", preferences.position_locked)
        visible = self.window.isVisible()
        changed = False
        self.window.setProperty("applying_overlay", True)
        try:
            for flag, enabled in ((Qt.WindowType.WindowStaysOnTopHint, preferences.pinned), (Qt.WindowType.WindowTransparentForInput, preferences.click_through and self.recovery_ready)):
                if bool(self.window.windowFlags() & flag) != enabled:
                    self.window.setWindowFlag(flag, enabled)
                    changed = True
            if changed and visible:
                self.window.show()
        finally:
            self.window.setProperty("applying_overlay", False)

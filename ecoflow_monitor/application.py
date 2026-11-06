from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from ecoflow_monitor.adapters.hotkeys import HotkeyManager
from ecoflow_monitor.adapters.accent import AccentWatcher
from ecoflow_monitor.adapters.windows import activate_window
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.integrations import hotkey_label
from ecoflow_monitor.ui.common import app_icon, elevation_prompt
from ecoflow_monitor.ui.mini import MiniWidget
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui.window import MonitorWindow


class Application(QObject):
    def __init__(self, settings_path: Path, native: bool = True, controller: Controller | None = None):
        super().__init__()
        self.native = native
        self.controller = controller if controller is not None else Controller(SettingsStore(settings_path))
        self.app = QApplication.instance()
        self.accent = AccentWatcher(self, native=native)
        self.app.ecoflow_accent = self.accent
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setWindowIcon(app_icon())
        self.window = MonitorWindow(self.controller, self.open_settings)
        self.accent.changed.connect(self.window.apply_appearance)
        self.window.setWindowIcon(app_icon())
        self.mini = MiniWidget(self.controller)
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self.hotkey)
        self._dialog = None
        self._stopped = False
        self._hotkey_notice = ""
        self._hotkey_signature = None
        self._elevation_checked = {}
        self._elevation_prompted = {}
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.menu = QMenu()
        self._helper_actions = {}
        self._menu_timer = QTimer(self)
        self._menu_timer.setInterval(500)
        self._menu_timer.timeout.connect(self._sync_menu)
        self.menu.aboutToShow.connect(self._sync_menu)
        self.menu.aboutToShow.connect(self._menu_timer.start)
        self.menu.aboutToHide.connect(self._menu_timer.stop)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.controller.preferences_changed.connect(self.preferences)
        self.controller.changed.connect(self._check_elevation)
        self.controller.changed.connect(self._sync_menu)
        self.window.visibility_changed.connect(self._sync_menu)
        self.controller.elevation_needed.connect(self._queue_elevation)
        self.app.aboutToQuit.connect(self.stop)
        self.preferences()

    def activate_existing_window(self):
        if self._stopped:
            return
        self.window.restore_for_activation()
        target = QApplication.activeModalWidget() or self._dialog or self.window
        if target is not self.window:
            target.setWindowState(target.windowState() & ~Qt.WindowState.WindowMinimized)
            target.show()
        activate_window(target)

    def preferences(self):
        self._elevation_checked.clear()
        enabled = {module.id: replace(module, auto_enabled=False)
                   for module in self.controller.settings.integrations if module.enabled}
        self._elevation_prompted = {identity: module for identity, module in self._elevation_prompted.items()
                                   if enabled.get(identity) == module}
        self.menu.clear()
        self.monitor_action = self.menu.addAction(t("monitor_widget"))
        self.monitor_action.setCheckable(True)
        self.monitor_action.triggered.connect(self.window.request_visibility)
        self.mini_action = self.menu.addAction(t("toggle_mini"))
        self.mini_action.setCheckable(True)
        self.mini_action.triggered.connect(lambda checked: self.controller.update_preferences(mini_enabled=checked))
        self.menu.addAction(t("settings"), self.open_settings)
        self._helper_actions = {}
        if self.native:
            for module in self.controller.settings.integrations:
                if module.enabled:
                    action = self.menu.addAction(t("integration_helper_start").format(name=module.label),
                                                 lambda checked=False, identity=module.id: self._offer_elevation(identity))
                    action.setCheckable(True)
                    self._helper_actions[module.id] = action
        self.menu.addSeparator()
        self.menu.addAction(t("exit"), self.app.quit)
        self.tray.setToolTip(t("app_title"))
        enabled_actions = {"toggle_monitor", "toggle_mini"}
        for module in self.controller.settings.integrations:
            if module.enabled:
                enabled_actions.add("auto:" + module.id)
                enabled_actions.update("profile:" + module.id + ":" + profile.id for profile in module.profiles)
        signature = (self.controller.settings.hotkeys, frozenset(enabled_actions), self.controller.settings.language)
        if self.native and signature != self._hotkey_signature:
            self._hotkey_signature = signature
            failures = self.hotkeys.configure(self.controller.settings.hotkeys, enabled_actions)
            self._hotkey_notice = t("hotkey_failed").format(actions=", ".join(hotkey_label(action, self.controller.settings.integrations) for action in failures)) if failures else ""
        self.window.hotkey_notice = self._hotkey_notice
        self.window.refresh()
        self.mini.refresh()
        self._sync_menu()

    def _sync_menu(self, *args):
        if self._stopped or not hasattr(self, "monitor_action"):
            return
        self.monitor_action.setChecked(self.window.isVisible())
        self.mini_action.setChecked(self.mini.isVisible())
        for module in self.controller.settings.integrations:
            action = self._helper_actions.get(module.id)
            if action is not None:
                ready = self.controller.integration_adapter.helper_ready(module)
                action.setChecked(ready)
                action.setEnabled(not ready and "elevate:" + module.id not in self.controller._active)

    def _check_elevation(self):
        if not self.native or self._stopped:
            return
        from ecoflow_monitor.adapters.elevation_windows import requires_administrator
        for module in self.controller.settings.integrations:
            observation = self.controller.integration_states[module.id].observation
            if not module.enabled:
                self._elevation_checked.pop(module.id, None)
                self._elevation_prompted.pop(module.id, None)
                continue
            module = replace(module, auto_enabled=False)
            if not observation.ready or self._elevation_checked.get(module.id) == module:
                continue
            self._elevation_checked[module.id] = module
            def check(path=observation.executable):
                return requires_administrator(path, Path(path).stat().st_mtime_ns)
            self.controller._submit("elevation_check:" + module.id, check)

    def _queue_elevation(self, identity):
        if self.native:
            QTimer.singleShot(0, lambda: self._offer_elevation(identity, automatic=True))

    def _offer_elevation(self, identity, automatic=False):
        if self._stopped:
            return
        module = next((item for item in self.controller.settings.integrations if item.id == identity and item.enabled), None)
        if module is None:
            return
        if self.controller.integration_adapter.helper_ready(module):
            return
        module = replace(module, auto_enabled=False)
        if automatic and self._elevation_prompted.get(identity) == module:
            return
        if "elevate:" + identity in self.controller._active:
            return
        self._elevation_prompted[identity] = module
        prompt = elevation_prompt(QApplication.activeModalWidget() or self.window, module.label)
        try:
            answer = prompt.exec()
        finally:
            prompt.deleteLater()
        current = next((item for item in self.controller.settings.integrations if item.id == identity), None)
        if (answer == QMessageBox.StandardButton.Yes and not self._stopped and current is not None
                and replace(current, auto_enabled=False) == module):
            self.controller._submit("elevate:" + identity, lambda: self.controller.integration_adapter.start_elevated(module))
            self._sync_menu()

    def start(self):
        if self.native and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        elif not self.controller.settings.monitor_visible and not self.controller.settings.mini_enabled:
            self.controller.update_preferences(monitor_visible=True)
            self.window._desired_visible = True
        self.window.startup()
        self.mini.refresh()
        if self.native:
            self.controller.start()

    def hotkey(self, action: str):
        if action == "toggle_monitor":
            self.window.request_visibility(not self.window._desired_visible)
        elif action == "toggle_mini":
            self.controller.update_preferences(mini_enabled=not self.controller.settings.mini_enabled)
        elif action.startswith("auto:"):
            integration_id = action.split(":", 1)[1]
            if integration_id not in self.controller.integration_states:
                return
            self.controller.set_auto(integration_id, not self.controller.module(integration_id).auto_enabled)
        elif action.startswith("profile:"):
            _, integration_id, profile_id = action.split(":")
            if integration_id not in self.controller.integration_states:
                return
            self.controller.request_profile(integration_id, profile_id)

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.hotkey("toggle_monitor")

    def open_settings(self):
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        dialog = SettingsWindow(self.controller, self.window)
        self._dialog = dialog
        dialog.exec()
        self._dialog = None
        dialog.deleteLater()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._menu_timer.stop()
        self.accent.stop()
        if getattr(self.app, "ecoflow_accent", None) is self.accent:
            del self.app.ecoflow_accent
        self.hotkeys.stop()
        self.tray.hide()
        self.window._save_geometry()
        self.mini._save_position()
        self.window._quitting = True
        self.window._geometry_timer.stop()
        self.mini._save_timer.stop()
        self.controller.stop()
        self.window.close()
        self.mini.close()

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from ecoflow_monitor.adapters.hotkeys import HotkeyError, HotkeyIssue, HotkeyManager, validate_bindings
from ecoflow_monitor.adapters.accent import AccentWatcher
from ecoflow_monitor.adapters.motion import MotionPreference
from ecoflow_monitor.adapters.windows import activate_window
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import SettingsStore
from ecoflow_monitor.ui.common import app_icon, elevation_prompt, font_family
from ecoflow_monitor.ui.adaptive_widget import AdaptiveWidget
from ecoflow_monitor.ui.motion import motion_driver
from ecoflow_monitor.ui.dashboard import Dashboard
from ecoflow_monitor.ui.polling import SensorDemand
from ecoflow_monitor.ui.components import DialogChrome
from ecoflow_monitor.ui.theme import menu_style
from ecoflow_monitor.ui.hotkey_editor import hotkey_error_text


class Application(QObject):
    def __init__(self, settings_path: Path, native: bool = True, controller: Controller | None = None, diagnostics=None):
        super().__init__()
        self.native = native
        self.controller = controller if controller is not None else Controller(SettingsStore(settings_path), diagnostics=diagnostics)
        self.app = QApplication.instance()
        font = QFont(font_family())
        font.setPixelSize(14)
        self.app.setFont(font)
        if not hasattr(self.app, "ecoflow_dialog_chrome"):
            self.app.ecoflow_dialog_chrome = DialogChrome(self.app)
            self.app.installEventFilter(self.app.ecoflow_dialog_chrome)
        self.accent = AccentWatcher(self, native=native)
        self.app.ecoflow_accent = self.accent
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setWindowIcon(app_icon())
        self.motion = MotionPreference(self, native)
        self.motion.changed.connect(self.controller.changed)
        self.motion_driver = motion_driver(self.controller, self)
        self.widget = AdaptiveWidget(self.controller, self.open_dashboard)
        self.widget.overlay.request = self.set_overlay
        self.main = self.mini = self.widget
        self.dashboard = Dashboard(self.controller, lambda: self.controller.update_widget(enabled=True))
        self.window = self.dashboard
        self.sensor_demand = SensorDemand(self.controller, self.dashboard, self.widget)
        self.dashboard.setWindowIcon(app_icon())
        self.dashboard.preflight = self.preflight_settings
        self.dashboard.preview_changed.connect(self.preview)
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self.hotkey)
        self._dialog = self.dashboard
        self._stopped = False
        self._started = False
        self._hotkey_notice = ""
        self._hotkey_signature = None
        self._elevation_checked = {}
        self._elevation_prompted = {}
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.menu = QMenu()
        self.menu.setStyleSheet(menu_style())
        self._helper_actions = {}
        self._menu_timer = QTimer(self)
        self._menu_timer.setInterval(500)
        self._menu_timer.timeout.connect(self._sync_menu)
        self.menu.aboutToShow.connect(self._sync_menu)
        self.menu.aboutToShow.connect(self._menu_timer.start)
        self.menu.aboutToHide.connect(self._menu_timer.stop)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.activate_existing_window)
        self.controller.credentials_expired.connect(self._notify_credentials_expired)
        self.controller.preferences_changed.connect(self.preferences)
        self.controller.changed.connect(self._check_elevation)
        self.controller.changed.connect(self._sync_menu)
        self.window.visibility_changed.connect(self._sync_menu)
        self.controller.elevation_needed.connect(self._queue_elevation)
        self.app.aboutToQuit.connect(self.stop)
        self.preferences()

    def _notify_credentials_expired(self):
        if self.native and self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(t("onepassword_cache_expired_title"), t("onepassword_cache_expired"),
                                  QSystemTrayIcon.MessageIcon.Warning, 10000)

    def activate_existing_window(self):
        if self._stopped:
            return
        self.open_dashboard()
        target = QApplication.activeModalWidget() or self.dashboard
        if target is not None:
            target.setWindowState(target.windowState() & ~Qt.WindowState.WindowMinimized)
            target.show()
        activate_window(target)

    def preferences(self):
        if self._stopped:
            return
        self._elevation_checked.clear()
        enabled = {module.id: replace(module, auto_enabled=False)
                   for module in self.controller.settings.integrations if module.enabled}
        self._elevation_prompted = {identity: module for identity, module in self._elevation_prompted.items()
                                   if enabled.get(identity) == module}
        self.menu.clear()
        self.monitor_action = self.menu.addAction(t("open_dashboard"))
        self.monitor_action.setCheckable(True)
        self.monitor_action.triggered.connect(self.window.request_visibility)
        self.main_action = self.menu.addAction(t("adaptive_widget"))
        self.main_action.setCheckable(True)
        self.main_action.triggered.connect(lambda checked: self.controller.update_widget(enabled=checked))
        self.mini_action = self.main_action
        self.menu.addAction(t("settings"), self.open_settings)
        self.menu.addAction(t("recover_widgets"), self.recover_widgets)
        for overlay in (self.widget.overlay,):
            submenu = self.menu.addMenu(t("widget_controls"))
            for key, label in (("pinned", "pin_widget"), ("position_locked", "position_lock"), ("click_through", "click_through")):
                control = submenu.addAction(t(label))
                control.setCheckable(True)
                control.setChecked(getattr(overlay.preferences, key))
                control.triggered.connect(lambda checked, target=overlay, name=key: target.change(**{name: checked}))
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
        enabled_actions = self._enabled_hotkey_actions(self.controller.settings)
        signature = (self.controller.settings.hotkeys, frozenset(enabled_actions), self.controller.settings.language)
        if self.native and signature != self._hotkey_signature:
            self._hotkey_signature = signature
            failures = self.hotkeys.configure(self.controller.settings.hotkeys, enabled_actions)
            self._hotkey_notice = self._registration_error(failures, self.controller.settings) if failures else ""
        recovery = next((binding for binding in self.controller.settings.hotkeys if binding.action == "recover_widgets"), None)
        ready = self.native and self.tray.isVisible() and recovery is not None and recovery.enabled and bool(recovery.combo) and "recover_widgets" in self.hotkeys._bindings.values()
        if self._started and not ready and self.controller.settings.widget.overlay.click_through:
            self.recover_widgets()
            self._hotkey_notice = t("click_through_unavailable")
        for overlay in (self.widget.overlay,):
            overlay.recovery_ready = ready
            overlay.apply()
        self.dashboard.monitoring.hotkey_notice = self._hotkey_notice
        self.dashboard.apply_appearance()
        self.widget.refresh()
        if self.dashboard is not None:
            self.dashboard.sync_runtime_overlays()
        self._sync_menu()

    def _sync_menu(self, *args):
        if self._stopped or not hasattr(self, "monitor_action"):
            return
        self.monitor_action.setChecked(self.window.isVisible())
        self.main_action.setChecked(self.widget.isVisible())
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
        self.widget.refresh()
        self._started = True
        self.preferences()
        self.open_dashboard()
        if self.native:
            self.controller.start()

    def hotkey(self, action: str):
        if action == "open_dashboard":
            self.open_dashboard()
        elif action == "recover_widgets":
            self.recover_widgets()
        elif action == "toggle_monitor":
            self.window.request_visibility(not self.window.isVisible())
        elif action in {"toggle_mini", "toggle_main"}:
            self.controller.update_widget(enabled=not self.controller.settings.widget.enabled)
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
            self.open_dashboard()

    def open_settings(self):
        self.open_dashboard(1)

    def open_dashboard(self, page=None):
        if self._stopped:
            return
        if page is not None:
            self.dashboard.navigation.setCurrentRow(page)
        self.dashboard.show()
        self.sensor_demand.refresh()
        self.dashboard.raise_()
        self.dashboard.activateWindow()

    def preview(self, settings):
        if self._stopped:
            return
        self.controller.preview = settings
        self.motion_driver.refresh()
        self.window.apply_appearance()
        self.widget.refresh()

    def preflight_settings(self, settings):
        previous = self.controller.settings
        validate_bindings(settings.hotkeys)
        actions = self._enabled_hotkey_actions(settings)
        wants_passthrough = settings.widget.overlay.click_through
        if wants_passthrough and (not self.native or not self.tray.isVisible()):
            raise ValueError("click_through_unavailable")
        binding = next((item for item in settings.hotkeys if item.action == "recover_widgets"), None)
        if wants_passthrough and (binding is None or not binding.enabled or not binding.combo):
            raise ValueError("click_through_unavailable")
        old_modules = {(module.id, tuple(profile.id for profile in module.profiles)) for module in previous.integrations if module.enabled}
        new_modules = {(module.id, tuple(profile.id for profile in module.profiles)) for module in settings.integrations if module.enabled}
        registration_changed = set(settings.hotkeys) != set(previous.hotkeys) or old_modules != new_modules
        needs_recovery = settings.widget.overlay.click_through and not previous.widget.overlay.click_through
        configured = self.native and (registration_changed or needs_recovery)
        if configured:
            failures = self.hotkeys.configure(settings.hotkeys, actions)
            if failures:
                raise self._hotkey_error(failures, settings)
        if wants_passthrough and "recover_widgets" not in self.hotkeys._bindings.values():
            if configured:
                self.hotkeys.configure(previous.hotkeys, self._enabled_hotkey_actions(previous))
            raise ValueError("click_through_unavailable")
        def rollback():
            if configured:
                self.hotkeys.configure(previous.hotkeys, self._enabled_hotkey_actions(previous))
        return rollback

    @staticmethod
    def _enabled_hotkey_actions(settings):
        actions = {"toggle_monitor", "toggle_mini", "open_dashboard", "recover_widgets", "toggle_main"}
        for module in settings.integrations:
            if module.enabled:
                actions.add("auto:" + module.id)
                actions.update("profile:" + module.id + ":" + profile.id for profile in module.profiles)
        return actions

    def _hotkey_error(self, failures, settings):
        issues = self.hotkeys.issues or tuple(HotkeyIssue(action, next((binding.combo for binding in settings.hotkeys if binding.action == action), ""), "hotkey_registration_failed") for action in failures)
        return HotkeyError(issues)

    def _registration_error(self, failures, settings):
        return hotkey_error_text(self._hotkey_error(failures, settings), settings.integrations)

    def set_overlay(self, kind, preferences):
        if kind not in {"widget", "main", "mini"}:
            return False
        previous_overlay = self.controller.settings.widget.overlay
        edits = tuple(name for name in preferences.__dataclass_fields__ if getattr(previous_overlay, name) != getattr(preferences, name))
        settings = replace(self.controller.settings, widget=replace(self.controller.settings.widget, overlay=preferences))
        try:
            rollback = self.preflight_settings(settings) if preferences.click_through else lambda: None
            try:
                self.controller.update_widget(overlay=preferences, overlay_fields=edits)
            except OSError:
                rollback()
                raise
        except (ValueError, OSError) as error:
            self._hotkey_notice = hotkey_error_text(error, settings.integrations) if isinstance(error, HotkeyError) else t(str(error))
            self.dashboard.monitoring.hotkey_notice = self._hotkey_notice
            self.dashboard.refresh()
            self.widget.overlay.apply()
            return False
        return True

    def recover_widgets(self):
        self.controller.update_widget(overlay=replace(self.controller.settings.widget.overlay, click_through=False), overlay_fields=("click_through",))

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.motion_driver.stop()
        if self.dashboard is not None:
            self.dashboard._draft_timer.stop()
            self.dashboard._log_status_timer.stop()
            self.dashboard._geometry_timer.stop()
            self.dashboard._screen_tracker.stop()
            self.dashboard._close_credentials()
            self.dashboard.hide()
        if self._dialog is not None and self._dialog is not self.dashboard:
            self._dialog.reject()
        self.controller.preview = None
        self._menu_timer.stop()
        self.accent.stop()
        self.motion.stop()
        if getattr(self.app, "ecoflow_accent", None) is self.accent:
            del self.app.ecoflow_accent
        self.hotkeys.stop()
        self.dashboard._focus_binding.stop()
        self.widget.stop()
        self.tray.hide()
        self.dashboard._save_geometry()
        self.dashboard._quitting = True
        self.window._geometry_timer.stop()
        self.controller.stop()
        self.window.close()
        self.widget.close()

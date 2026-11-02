import hashlib
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from ecoflow_monitor.adapters.hotkeys import HotkeyManager
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
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setWindowIcon(app_icon())
        self.window = MonitorWindow(self.controller, self.open_settings)
        self.window.setWindowIcon(app_icon())
        self.mini = MiniWidget(self.controller)
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self.hotkey)
        self._dialog = None
        self._stopped = False
        self._hotkey_notice = ""
        self._hotkey_signature = None
        self._server = None
        self._connections = set()
        self._elevation_checked = {}
        self._elevation_prompted = {}
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.controller.preferences_changed.connect(self.preferences)
        self.controller.changed.connect(self._check_elevation)
        self.controller.elevation_needed.connect(self._queue_elevation)
        self.app.aboutToQuit.connect(self.stop)
        self.preferences()

    def ensure_single_instance(self, path: Path) -> bool:
        name = "EcoFlowMonitor-" + hashlib.sha256(str(path.resolve()).casefold().encode()).hexdigest()[:24]
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if server.listen(name):
            self._server = server
            server.newConnection.connect(self._new_connection)
            return True
        client = QLocalSocket()
        client.connectToServer(name)
        if client.waitForConnected(500):
            client.write(b"show")
            if client.bytesToWrite() > 0:
                client.waitForBytesWritten(500)
            client.disconnectFromServer()
        else:
            QMessageBox.warning(None, t("error_title"), t("operation_failed"))
        return False

    def _new_connection(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._connections.add(socket)
            def receive(connection=socket):
                if bytes(connection.readAll()) == b"show":
                    self.window.request_visibility(True)
            def release(connection=socket):
                self._connections.discard(connection)
                connection.deleteLater()
            socket.readyRead.connect(receive)
            socket.disconnected.connect(release)
            if socket.bytesAvailable():
                receive()

    def preferences(self):
        self._elevation_checked.clear()
        enabled = {module.id: replace(module, auto_enabled=False)
                   for module in self.controller.settings.integrations if module.enabled}
        self._elevation_prompted = {identity: module for identity, module in self._elevation_prompted.items()
                                   if enabled.get(identity) == module}
        self.menu.clear()
        self.menu.addAction(t("show_monitor"), lambda: self.window.request_visibility(True))
        self.menu.addAction(t("toggle_mini"), lambda: self.hotkey("toggle_mini"))
        self.menu.addAction(t("settings"), self.open_settings)
        if self.native:
            for module in self.controller.settings.integrations:
                if module.enabled:
                    self.menu.addAction(t("integration_helper_start").format(name=module.label),
                                        lambda checked=False, identity=module.id: self._offer_elevation(identity))
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
            if failures:
                self._hotkey_notice = t("hotkey_failed").format(actions=", ".join(hotkey_label(action, self.controller.settings.integrations) for action in failures))
                self.controller.notice = self._hotkey_notice
                self.controller.changed.emit()
            elif self._hotkey_notice and self.controller.notice == self._hotkey_notice:
                self.controller.notice = ""
                self._hotkey_notice = ""
                self.controller.changed.emit()
        self.mini.refresh()

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
        self.hotkeys.stop()
        self.tray.hide()
        self.window._save_geometry()
        self.mini._save_position()
        self.window._quitting = True
        self.window._geometry_timer.stop()
        self.mini._save_timer.stop()
        self.controller.stop()
        if self._server is not None:
            self._server.close()
        self.window.close()
        self.mini.close()

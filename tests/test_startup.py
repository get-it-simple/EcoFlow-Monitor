from dataclasses import replace
from types import SimpleNamespace
import subprocess

import pytest

from ecoflow_monitor.adapters.startup import RUN_KEY, VALUE_NAME, WindowsStartup
from ecoflow_monitor.application import Application
from ecoflow_monitor.adapters.system_activity import SystemActivityWatcher
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.settings import AppSettings, SettingsStore
from qt_helpers import close_runtime


class Registry:
    HKEY_CURRENT_USER = 1
    KEY_READ = 2
    KEY_SET_VALUE = 4
    REG_SZ = 1

    def __init__(self):
        self.value = None
        self.fail = False

    def OpenKey(self, root, path, reserved, access):
        assert path == RUN_KEY
        return self

    CreateKeyEx = OpenKey

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def QueryValueEx(self, key, name):
        assert name == VALUE_NAME
        if self.value is None:
            raise FileNotFoundError
        return self.value

    def SetValueEx(self, key, name, reserved, kind, value):
        if self.fail:
            raise PermissionError
        self.value = value, kind

    def DeleteValue(self, key, name):
        if self.fail:
            raise PermissionError
        self.value = None


def test_startup_quotes_executable_and_restores_previous_registration(tmp_path):
    registry = Registry()
    executable = tmp_path / "Program Files" / "EcoFlowMonitor.exe"
    startup = WindowsStartup(registry, executable)
    rollback = startup.configure(True)
    assert registry.value == (subprocess.list2cmdline([str(executable.resolve()), "--autostart"]), registry.REG_SZ)
    rollback()
    assert registry.value is None
    startup.configure(True)
    rollback = startup.configure(False)
    assert registry.value is None
    rollback()
    assert registry.value[0] == startup.command
    registry.value = ('"C:\\Other\\EcoFlowMonitor.exe" --autostart', 1)
    startup.configure(False)
    assert "Other" in registry.value[0]


def test_startup_registry_failure_does_not_change_existing_value(tmp_path):
    registry = Registry()
    registry.value = ("previous", 1)
    registry.fail = True
    with pytest.raises(PermissionError):
        WindowsStartup(registry, tmp_path / "app.exe").configure(True)
    assert registry.value == ("previous", 1)


def test_startup_and_disclosures_roundtrip_and_old_settings_default_off(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = replace(AppSettings(), start_with_windows=True, ac_collapsed=True, other_collapsed=True)
    store.save(settings)
    assert store.load() == settings
    data = settings.to_dict()
    del data["startup"], data["monitoring"]
    restored = AppSettings.from_dict(data)
    assert not restored.start_with_windows and not restored.ac_collapsed and not restored.other_collapsed
    data["startup"] = {"enabled": "false"}
    with pytest.raises(ValueError):
        AppSettings.from_dict(data)


def test_background_start_waits_for_user_then_uses_startup_confirmation(tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    runtime = Application(tmp_path / "settings.json", native=False)
    try:
        monkeypatch.setattr(runtime.activity_watcher, SystemActivityWatcher.start.__name__, lambda: None)
        monkeypatch.setattr(runtime, "preferences", lambda: None)
        monkeypatch.setattr(runtime.controller, Controller.start.__name__, lambda **kwargs: None)
        tray = SimpleNamespace(show=lambda: None, isVisible=lambda: True, showMessage=lambda *args: messages.append(args))
        monkeypatch.setattr(runtime, "tray", tray)
        monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
        monkeypatch.setattr(QSystemTrayIcon, "supportsMessages", lambda: True)
        messages, confirmations = [], []
        monkeypatch.setattr(runtime, "_startup_consent", lambda: confirmations.append(True))
        runtime.native = True
        runtime.start(autostart=True)
        assert not runtime.dashboard.isVisible()
        assert runtime._startup_pending and runtime.controller.startup_pending
        assert len(messages) == 1 and not confirmations
        runtime.activate_existing_window()
        assert runtime.dashboard.isVisible()
        assert confirmations == [True]
        assert not runtime._startup_pending
        runtime.open_dashboard()
        assert confirmations == [True]
    finally:
        runtime.native = False
        monkeypatch.undo()
        close_runtime(runtime, qapp)


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_startup_save_failure_restores_registration_and_keeps_draft(tmp_path, qapp, monkeypatch, rollback_fails):
    from ecoflow_monitor import i18n
    from ecoflow_monitor.adapters import startup
    from ecoflow_monitor.settings import SettingsError
    from PySide6.QtWidgets import QMessageBox
    runtime = Application(tmp_path / "settings.json", native=False)
    registry = Registry()
    adapter = WindowsStartup(registry, tmp_path / "app.exe")
    try:
        monkeypatch.setattr(startup, "available", lambda: True)
        monkeypatch.setattr(startup, "WindowsStartup", lambda: adapter)
        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
        runtime.dashboard.start_with_windows.setChecked(True)
        def fail(*args, **kwargs):
            registry.fail = rollback_fails
            raise SettingsError("settings_save_error")
        monkeypatch.setattr(runtime.controller, Controller.apply_settings.__name__, fail)
        runtime.native = True
        runtime.dashboard.save()
        if rollback_fails:
            assert registry.value[0] == adapter.command
            assert i18n.t("settings_rollback_failed") in warnings[-1]
        else:
            assert registry.value is None
        assert runtime.dashboard.start_with_windows.isChecked()
        assert not runtime.controller.settings.start_with_windows
    finally:
        runtime.native = False
        close_runtime(runtime, qapp)


def test_declined_background_consent_remains_actionable_without_automatic_retry(tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from qt_helpers import wait_until
    runtime = Application(tmp_path / "settings.json", native=False)
    controller = runtime.controller
    try:
        controller.settings = replace(controller.settings, serial="SYNTHETIC")
        controller.startup_pending = True
        controller.access_allowed = False
        runtime._automatic_helpers_allowed = False
        runtime._startup_pending = True
        monkeypatch.setattr(controller, controller._read_credentials.__name__, lambda *args: pytest.fail("Credentials read without consent"))
        runtime.open_dashboard()
        wait_until(qapp, lambda: runtime.confirmations.dialog is not None)
        runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
        qapp.processEvents()
        controller.tick()
        assert controller.startup_pending and not controller.access_allowed
        assert runtime.confirmations.dialog is None and not controller._active
        runtime.open_dashboard()
        qapp.processEvents()
        assert runtime.confirmations.dialog is None
        runtime.dashboard.monitoring.notifications.refresh(controller)
        indicator = runtime.dashboard.monitoring.notifications
        indicator.click()
        indicator.dialog.buttons["startup"].click()
        wait_until(qapp, lambda: runtime.confirmations.dialog is not None)
        runtime.confirmations.dialog.done(QMessageBox.StandardButton.No)
    finally:
        close_runtime(runtime, qapp)

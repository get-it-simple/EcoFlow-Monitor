from pathlib import Path
import subprocess
import sys


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "EcoFlowMonitor"


def available() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


class WindowsStartup:
    def __init__(self, registry=None, executable=None):
        if registry is None:
            import winreg
            registry = winreg
        self.registry = registry
        self.command = subprocess.list2cmdline([str(Path(executable or sys.executable).resolve()), "--autostart"])

    def read(self):
        registry = self.registry
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
                return registry.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return None

    def restore(self, previous):
        registry = self.registry
        with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
            if previous is None:
                try:
                    registry.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
            else:
                registry.SetValueEx(key, VALUE_NAME, 0, previous[1], previous[0])

    def configure(self, enabled):
        previous = self.read()
        if enabled:
            self.restore((self.command, self.registry.REG_SZ))
        elif previous is not None and previous[0] == self.command:
            self.restore(None)
        return lambda: self.restore(previous)

import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication

from ecoflow_monitor.settings import HotkeyBinding


MODIFIERS = {"ctrl": 2, "alt": 1, "shift": 4, "win": 8}
KEYS = {**{chr(code): code - 32 for code in range(97, 123)}, **{str(value): 48 + value for value in range(10)}, **{f"num{value}": 96 + value for value in range(10)}, **{f"f{value}": 111 + value for value in range(1, 25)}, "space": 32, "enter": 13, "esc": 27, "up": 38, "down": 40, "left": 37, "right": 39, "home": 36, "end": 35, "insert": 45, "delete": 46, "page up": 33, "page down": 34, "tab": 9}


def parse_combo(combo: str) -> tuple[int, int]:
    parts = [part.strip().casefold() for part in combo.split("+")]
    if len(set(parts)) != len(parts):
        raise ValueError("invalid_hotkey")
    keys = [part for part in parts if part not in MODIFIERS]
    if len(keys) != 1 or keys[0] not in KEYS:
        raise ValueError("invalid_hotkey")
    return sum(MODIFIERS[part] for part in parts if part in MODIFIERS), KEYS[keys[0]]


def validate_bindings(bindings: tuple[HotkeyBinding, ...]) -> None:
    used = set()
    for binding in bindings:
        if not binding.enabled:
            continue
        parsed = parse_combo(binding.combo)
        if parsed in used:
            raise ValueError("hotkey_conflict")
        used.add(parsed)


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    triggered = Signal(str)

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._bindings = {}
        self._next_id = 1
        self._installed = False

    def stop(self) -> None:
        if sys.platform == "win32":
            user = ctypes.WinDLL("user32")
            for identifier in self._bindings:
                user.UnregisterHotKey(None, identifier)
        self._bindings.clear()
        app = QApplication.instance()
        if app is not None and self._installed:
            app.removeNativeEventFilter(self)
        self._installed = False

    def configure(self, bindings: tuple[HotkeyBinding, ...], msi_enabled: bool) -> tuple[str, ...]:
        self.stop()
        if sys.platform != "win32":
            return ("hotkeys_unavailable",)
        user = ctypes.WinDLL("user32")
        user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user.RegisterHotKey.restype = wintypes.BOOL
        failures = []
        for binding in bindings:
            if not binding.enabled or (not msi_enabled and binding.action not in {"toggle_monitor", "toggle_mini"}):
                continue
            try:
                modifiers, key = parse_combo(binding.combo)
            except ValueError:
                failures.append(binding.action)
                continue
            identifier = self._next_id
            self._next_id += 1
            if user.RegisterHotKey(None, identifier, modifiers | 0x4000, key):
                self._bindings[identifier] = binding.action
            else:
                failures.append(binding.action)
        app = QApplication.instance()
        if app is not None and self._bindings:
            app.installNativeEventFilter(self)
            self._installed = True
        return tuple(failures)

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            native = wintypes.MSG.from_address(int(message))
            if native.message == 0x0312 and native.wParam in self._bindings:
                self.triggered.emit(self._bindings[native.wParam])
                return True, 0
        return False, 0

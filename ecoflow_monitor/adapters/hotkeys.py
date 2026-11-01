import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication

from ecoflow_monitor.settings import HotkeyBinding
from ecoflow_monitor.adapters.hotkey_binding import MOD_NOREPEAT, combo_to_hotkey


def parse_combo(combo: str) -> tuple[int, int]:
    tokens = [part.strip().casefold() for part in combo.split("+")]
    if len(tokens) != len(set(tokens)):
        raise ValueError("invalid_hotkey")
    return combo_to_hotkey(combo)


def validate_bindings(bindings: tuple[HotkeyBinding, ...]) -> None:
    used = set()
    for binding in bindings:
        if not binding.enabled or not binding.combo:
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
            user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user.UnregisterHotKey.restype = wintypes.BOOL
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
        used = set()
        for binding in bindings:
            if not binding.enabled or not binding.combo or (not msi_enabled and binding.action not in {"toggle_monitor", "toggle_mini"}):
                continue
            try:
                modifiers, key = parse_combo(binding.combo)
                if (modifiers, key) in used:
                    raise ValueError("hotkey_conflict")
            except ValueError:
                failures.append(binding.action)
                continue
            used.add((modifiers, key))
            identifier = self._next_id
            self._next_id += 1
            if user.RegisterHotKey(None, identifier, modifiers | MOD_NOREPEAT, key):
                self._bindings[identifier] = binding.action
            else:
                failures.append(binding.action)
        app = QApplication.instance()
        if app is not None and self._bindings:
            app.installNativeEventFilter(self)
            self._installed = True
        return tuple(failures)

    def _handle_hotkey(self, identifier: int) -> None:
        action = self._bindings.get(identifier)
        if action is not None:
            self.triggered.emit(action)

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            native = wintypes.MSG.from_address(int(message))
            if native.message == 0x0312 and native.wParam in self._bindings:
                self._handle_hotkey(native.wParam)
                return True, 0
        return False, 0

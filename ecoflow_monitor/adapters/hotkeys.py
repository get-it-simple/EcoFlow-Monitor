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
        self._combos = {}
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
        self._combos.clear()
        app = QApplication.instance()
        if app is not None and self._installed:
            app.removeNativeEventFilter(self)
        self._installed = False

    def configure(self, bindings: tuple[HotkeyBinding, ...], enabled_actions: set[str]) -> tuple[str, ...]:
        if sys.platform != "win32":
            return ("hotkeys_unavailable",)
        user = ctypes.WinDLL("user32")
        user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user.RegisterHotKey.restype = wintypes.BOOL
        failures = []
        used = set()
        requested = []
        for binding in bindings:
            if not binding.enabled or not binding.combo or binding.action not in enabled_actions:
                continue
            try:
                modifiers, key = parse_combo(binding.combo)
                if (modifiers, key) in used:
                    raise ValueError("hotkey_conflict")
            except ValueError:
                failures.append(binding.action)
                continue
            used.add((modifiers, key))
            requested.append((binding.action, modifiers, key))
        if failures:
            return tuple(failures)
        existing = {combo: identifier for identifier, combo in self._combos.items()}
        registered = {}
        bindings_by_id = {}
        combos_by_id = {}
        for action, modifiers, key in requested:
            combo = (modifiers, key)
            identifier = existing.get(combo)
            if identifier is None:
                identifier = self._next_id
                self._next_id += 1
                if not user.RegisterHotKey(None, identifier, modifiers | MOD_NOREPEAT, key):
                    failures.append(action)
                    break
                registered[identifier] = combo
            bindings_by_id[identifier] = action
            combos_by_id[identifier] = combo
        user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user.UnregisterHotKey.restype = wintypes.BOOL
        if failures:
            for identifier in registered:
                user.UnregisterHotKey(None, identifier)
            return tuple(failures)
        for identifier in self._bindings.keys() - bindings_by_id.keys():
            user.UnregisterHotKey(None, identifier)
        self._bindings = bindings_by_id
        self._combos = combos_by_id
        app = QApplication.instance()
        if app is not None and self._bindings and not self._installed:
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

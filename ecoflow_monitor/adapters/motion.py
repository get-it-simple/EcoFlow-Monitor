import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication


def system_animations_enabled():
    if sys.platform != "win32":
        return True
    try:
        user = ctypes.WinDLL("user32")
        user.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        user.SystemParametersInfoW.restype = wintypes.BOOL
        enabled = wintypes.BOOL(True)
        if user.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    except OSError:
        return True
    return True


class MotionPreference(QObject, QAbstractNativeEventFilter):
    changed = Signal()

    def __init__(self, parent, native):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self.native = native
        self.app = QApplication.instance()
        if native:
            self.app.installNativeEventFilter(self)
        self.refresh()

    def refresh(self):
        enabled = system_animations_enabled() if self.native else True
        if self.app.property("system_animations") != enabled:
            self.app.setProperty("system_animations", enabled)
            self.changed.emit()

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG") and wintypes.MSG.from_address(int(message)).message == 0x001A:
            self.refresh()
        return False, 0

    def stop(self):
        if self.native:
            self.app.removeNativeEventFilter(self)

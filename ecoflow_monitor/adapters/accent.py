from ctypes import wintypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from ecoflow_monitor.adapters import reference_effects


class AccentWatcher(QObject, QAbstractNativeEventFilter):
    changed = Signal()

    def __init__(self, parent=None, native=True):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self.color = reference_effects.get_accent_color()
        self._app = QApplication.instance()
        if not isinstance(self._app, QApplication):
            raise RuntimeError("AccentWatcher requires QApplication")
        self._installed = native and sys.platform == "win32"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self._app.paletteChanged.connect(self.schedule)
        if self._installed:
            self._app.installNativeEventFilter(self)

    def schedule(self, *args):
        self._timer.start(0)

    def refresh(self):
        color = reference_effects.get_accent_color()
        if color != self.color:
            self.color = color
            self.changed.emit()

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            native = wintypes.MSG.from_address(int(message))
            if native.message in (0x0320, 0x001A, 0x031A):
                self.schedule()
        return False, 0

    def stop(self):
        self._timer.stop()
        self._app.paletteChanged.disconnect(self.schedule)
        if self._installed:
            self._app.removeNativeEventFilter(self)
            self._installed = False
